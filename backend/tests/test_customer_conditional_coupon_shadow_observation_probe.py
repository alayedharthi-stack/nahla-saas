"""Regression tests for conditional-coupon shadow observation probe operator."""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.operators import (  # noqa: E402
    customer_conditional_coupon_shadow_observation as probe,
)
from scripts.operators.customer_conditional_coupon_shadow_observation_contract import (  # noqa: E402
    APP_CONTAINER_SYS_PATH,
    CODE_COMPOSE_FLAG_ENABLED,
    CODE_SHADOW_FLAG_NOT_ENABLED,
    DEPLOYMENT_APP_ROOT,
    INVALID_LEGACY_SESSION_LOCAL_IMPORT,
    OBSERVATION_PROBE_MESSAGE,
    REPORT_SCHEMA_VERSION,
    VALID_SESSION_LOCAL_IMPORT,
)


def test_app_container_sys_path_matches_deployed_layout() -> None:
    entries = probe.app_container_sys_path_entries(_REPO)
    assert entries[0] == str(_REPO.resolve())
    assert entries[1] == str((_REPO / "backend").resolve())
    assert entries[2] == str((_REPO / "database").resolve())
    assert APP_CONTAINER_SYS_PATH == (
        DEPLOYMENT_APP_ROOT,
        f"{DEPLOYMENT_APP_ROOT}/backend",
        f"{DEPLOYMENT_APP_ROOT}/database",
    )


def test_legacy_session_local_import_fails_in_app_container_layout() -> None:
    with probe.with_app_container_paths(_REPO):
        import_check = probe.verify_session_local_import()
        assert import_check["session_local_import_ok"] is True
        assert import_check["legacy_database_package_import_ok"] is False

        with pytest.raises(ImportError):
            exec(INVALID_LEGACY_SESSION_LOCAL_IMPORT, {"__name__": "legacy_probe"})

        namespace: dict[str, object] = {}
        exec(VALID_SESSION_LOCAL_IMPORT, namespace)
        assert callable(namespace["SessionLocal"])


def test_probe_source_never_embeds_legacy_session_local_import() -> None:
    assert probe.probe_source_uses_valid_session_local_import() is True
    source = Path(probe.__file__).read_text(encoding="utf-8")
    assert "from database.session import SessionLocal" in source
    assert "from database import SessionLocal\n" not in source


def test_default_off_probe_zero_io_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        raising=False,
    )
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
        raising=False,
    )
    result = probe.execute_default_off_probe(app_root=_REPO)
    assert result["ok"] is True
    assert result["phase"] == "default_off_verify"
    assert result["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert result["shadow_enabled"] is False
    assert result["zero_io_contract"] is True
    assert result["facts_count"] == 0


def test_shadow_observation_requires_process_scoped_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        raising=False,
    )
    result = probe.execute_shadow_observation_probe(
        MagicMock(),
        conversation=SimpleNamespace(customer_id=1),
        app_root=_REPO,
    )
    assert result == {"ok": False, "code": CODE_SHADOW_FLAG_NOT_ENABLED}


def test_shadow_observation_rejects_compose_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )
    monkeypatch.setenv("NAHLA_TRUSTED_CONTEXT_COUPON_OFFER_COMPOSE_ENABLED", "true")
    result = probe.execute_shadow_observation_probe(
        MagicMock(),
        conversation=SimpleNamespace(customer_id=1),
        app_root=_REPO,
    )
    assert result == {"ok": False, "code": CODE_COMPOSE_FLAG_ENABLED}


def test_process_scoped_shadow_observation_no_materialise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )
    for flag in (
        "NAHLA_TRUSTED_CONTEXT_COUPON_OFFER_COMPOSE_ENABLED",
        "NAHLA_TRUSTED_CONTEXT_PRODUCT_SALE_OFFER_COMPOSE_ENABLED",
        "NAHLA_TRUSTED_CONTEXT_GENERAL_OFFER_DISCOVERY_COMPOSE_ENABLED",
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_COMPOSE_ENABLED",
    ):
        monkeypatch.delenv(flag, raising=False)

    conversation = SimpleNamespace(customer_id=42, id=7)
    with patch(
        "modules.ai.brain.truth_surface.customer_conditional_coupon_loader.scan_conditional_targets",
        return_value=[],
    ):
        result = probe.execute_shadow_observation_probe(
            MagicMock(),
            conversation=conversation,
            message=OBSERVATION_PROBE_MESSAGE,
            app_root=_REPO,
        )

    assert result["ok"] is True
    assert result["phase"] == "shadow_observation"
    assert result["guards"]["materialise_for_customer_called"] is False
    assert result["guards"]["compose_flags_enabled"] is False


def test_shadow_observation_imports_loader_under_app_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NAHLA_TRUSTED_CONTEXT_CUSTOMER_CONDITIONAL_COUPON_SHADOW_ENABLED",
        "true",
    )
    with probe.with_app_container_paths(_REPO):
        module = importlib.import_module(
            "modules.ai.brain.truth_surface.customer_conditional_coupon_loader"
        )
        assert module.__file__
        assert str((_REPO / "backend").resolve()) in str(Path(module.__file__).resolve())


def test_cli_default_off_emits_closed_json() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.operators.customer_conditional_coupon_shadow_observation",
            "default-off",
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip().startswith("{")
    assert '"zero_io_contract":true' in proc.stdout.replace(" ", "")


def test_regression_staging_observation_session_local_import_error() -> None:
    """Mirrors deploy ``599b4297`` abort before inline probe import fix."""
    with probe.with_app_container_paths(_REPO):
        with pytest.raises(ImportError):
            exec(INVALID_LEGACY_SESSION_LOCAL_IMPORT, {"__name__": "staging_regression"})
