"""Regression tests for fixed acceptance-tenant policy (tenant 33)."""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from core.tenant import DEFAULT_AI, STORE_AI_MODE_TEST, merge_ai_defaults
from modules.ai.brain.persona.flags import (
    is_persona_composer_enforce_enabled,
    persona_composer_allowlist_result,
)
from modules.platform.fixed_tenant_policy import (
    ACCEPTANCE_TENANT_ID,
    scan_fixed_tenant_violations,
    scan_python_file,
)


_GENERIC_TENANT = 8201
_TEST_PHONE = "966542980511"


def _enabled_ai(**overrides: object) -> dict:
    base = {
        "persona_composer_enabled": True,
        "store_ai_mode": STORE_AI_MODE_TEST,
        "ai_test_allowed_numbers": [_TEST_PHONE],
    }
    base.update(overrides)
    return merge_ai_defaults(base)


class TestPersonaComposerAllowlistObsolete:
    def test_default_config_keeps_empty_legacy_key(self) -> None:
        assert DEFAULT_AI["persona_composer_allowlist_tenants"] == []

    def test_stale_tenant_id_list_is_not_a_gate(self) -> None:
        ai = _enabled_ai()
        for tenant_id in (ACCEPTANCE_TENANT_ID, 1, 77, _GENERIC_TENANT):
            assert is_persona_composer_enforce_enabled(
                tenant_id=tenant_id,
                customer_phone=_TEST_PHONE,
                ai_settings=ai,
            )
            assert persona_composer_allowlist_result(
                tenant_id=tenant_id,
                customer_phone=_TEST_PHONE,
                ai_settings=ai,
            ) == "allowed"

    def test_stale_copy_of_33_does_not_create_cross_tenant_privilege(self) -> None:
        ai = _enabled_ai(persona_composer_allowlist_tenants=[ACCEPTANCE_TENANT_ID])
        assert is_persona_composer_enforce_enabled(
            tenant_id=1,
            customer_phone=_TEST_PHONE,
            ai_settings=ai,
        )
        disabled = _enabled_ai(
            persona_composer_enabled=False,
            persona_composer_allowlist_tenants=[ACCEPTANCE_TENANT_ID],
        )
        assert not is_persona_composer_enforce_enabled(
            tenant_id=1,
            customer_phone=_TEST_PHONE,
            ai_settings=disabled,
        )
        assert not is_persona_composer_enforce_enabled(
            tenant_id=ACCEPTANCE_TENANT_ID,
            customer_phone=_TEST_PHONE,
            ai_settings=disabled,
        )

    def test_ha_phone_still_required(self) -> None:
        ai = _enabled_ai(persona_composer_allowlist_tenants=[_GENERIC_TENANT])
        assert not is_persona_composer_enforce_enabled(
            tenant_id=_GENERIC_TENANT,
            customer_phone="966500000099",
            ai_settings=ai,
        )

    def test_malformed_allowlist_is_ignored(self) -> None:
        ai = merge_ai_defaults(
            {
                "persona_composer_enabled": True,
                "store_ai_mode": STORE_AI_MODE_TEST,
                "ai_test_allowed_numbers": [_TEST_PHONE],
                "persona_composer_allowlist_tenants": "33",
            }
        )
        assert is_persona_composer_enforce_enabled(
            tenant_id=ACCEPTANCE_TENANT_ID,
            customer_phone=_TEST_PHONE,
            ai_settings=ai,
        )


class TestFixedTenantStaticScanner:
    def test_production_runtime_has_no_tenant_33_dependency(self) -> None:
        violations = scan_fixed_tenant_violations(zone="production")
        assert violations == [], "\n".join(v.format() for v in violations)

    def test_ops_scripts_have_no_implicit_tenant_33_defaults(self) -> None:
        violations = scan_fixed_tenant_violations(zone="ops")
        assert violations == [], "\n".join(v.format() for v in violations)

    def test_scanner_catches_forbidden_runtime_literal(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        bad = repo / "backend" / "core"
        bad.mkdir(parents=True)
        target = bad / "evil.py"
        target.write_text("TENANT = 33\n", encoding="utf-8")

        import modules.platform.fixed_tenant_policy as policy

        original = policy.REPO_ROOT
        policy.REPO_ROOT = repo
        try:
            hits = scan_python_file(target)
            assert hits
            assert hits[0].kind == "numeric_literal"
        finally:
            policy.REPO_ROOT = original

    def test_scanner_ignores_meta_subcode_context(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        mod = repo / "backend" / "routers"
        mod.mkdir(parents=True)
        target = mod / "api.py"
        target.write_text(
            textwrap.dedent(
                """
                def heal(err_subcode):
                    return err_subcode == 33
                """
            ).lstrip(),
            encoding="utf-8",
        )

        import modules.platform.fixed_tenant_policy as policy

        original = policy.REPO_ROOT
        policy.REPO_ROOT = repo
        try:
            assert scan_python_file(target) == []
        finally:
            policy.REPO_ROOT = original

    def test_scanner_ignores_acceptance_test_paths(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        tests = repo / "backend" / "tests"
        tests.mkdir(parents=True)
        target = tests / "test_acceptance.py"
        target.write_text("TENANT = 33\n", encoding="utf-8")

        import modules.platform.fixed_tenant_policy as policy

        original = policy.REPO_ROOT
        policy.REPO_ROOT = repo
        try:
            assert scan_python_file(target) == []
        finally:
            policy.REPO_ROOT = original

    def test_scanner_flags_ops_argparse_default(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        scripts = repo / "backend" / "scripts"
        scripts.mkdir(parents=True)
        target = scripts / "ops.py"
        target.write_text(
            textwrap.dedent(
                """
                import argparse
                p = argparse.ArgumentParser()
                p.add_argument("--tenant", type=int, default=33)
                """
            ).lstrip(),
            encoding="utf-8",
        )

        import modules.platform.fixed_tenant_policy as policy

        original = policy.REPO_ROOT
        policy.REPO_ROOT = repo
        try:
            hits = scan_python_file(target)
            assert hits
            assert hits[0].severity.value == "fail_ops_implicit_default"
        finally:
            policy.REPO_ROOT = original

    def test_flags_module_has_no_hidden_fallback(self) -> None:
        from modules.ai.brain.persona import flags as persona_flags

        src = Path(persona_flags.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == ACCEPTANCE_TENANT_ID:
                pytest.fail("persona flags still embed acceptance tenant fallback")
