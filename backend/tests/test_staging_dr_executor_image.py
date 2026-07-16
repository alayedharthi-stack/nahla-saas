"""Deterministic guards for the unified staging DR executor image."""
from __future__ import annotations

from pathlib import Path

EXECUTOR_ROOT = Path("ops/staging_dr_executor")
SCRIPTS_DIR = EXECUTOR_ROOT / "scripts"

LEGACY_OPERATIONAL_SCRIPTS = (
    "backup.sh",
    "restore_verify.sh",
    "target_preflight.sh",
    "common.sh",
    "idle.sh",
)

PARITY_SCRIPTS = ("verify_canonical_parity.sh",)

REQUIRED_SCRIPTS = LEGACY_OPERATIONAL_SCRIPTS + PARITY_SCRIPTS


def test_executor_tracks_all_legacy_operational_scripts() -> None:
    present = {path.name for path in SCRIPTS_DIR.glob("*.sh")}
    assert set(REQUIRED_SCRIPTS).issubset(present)


def test_legacy_scripts_require_staging_identity() -> None:
    for name in ("backup.sh", "restore_verify.sh", "target_preflight.sh", "verify_canonical_parity.sh"):
        script = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
        assert "require_staging_identity" in script
        assert "desirable-growth" in (SCRIPTS_DIR / "common.sh").read_text(encoding="utf-8")


def test_backup_script_preserves_operational_contract() -> None:
    script = (SCRIPTS_DIR / "backup.sh").read_text(encoding="utf-8")
    assert "NAHLA_STG_DR_ENCRYPT_KEY" in script
    assert "NAHLA_STG_DR_BUCKET" in script
    assert "aws s3 cp" in script
    assert "pg_dump" in script
    assert "openssl enc -aes-256-cbc -pbkdf2" in script
    assert "backup_storage_class=railway_bucket_private" in script


def test_restore_script_preserves_operational_contract() -> None:
    script = (SCRIPTS_DIR / "restore_verify.sh").read_text(encoding="utf-8")
    assert "NAHLA_STG_DR_OBJECT_KEY" in script
    assert "aws s3 cp" in script
    assert "pg_restore" in script
    assert "restore_alembic_revision=" in script


def test_target_preflight_fails_closed_on_nonempty_target() -> None:
    script = (SCRIPTS_DIR / "target_preflight.sh").read_text(encoding="utf-8")
    assert "target_empty=false" in script
    assert "exit 2" in script


def test_idle_entrypoint_is_long_running_for_ssh() -> None:
    idle = (SCRIPTS_DIR / "idle.sh").read_text(encoding="utf-8")
    dockerfile = (EXECUTOR_ROOT / "Dockerfile").read_text(encoding="utf-8")
    railway = (EXECUTOR_ROOT / "railway.toml").read_text(encoding="utf-8")
    assert "exec sleep infinity" in idle
    assert "nahla-stg-dr-job ready" in idle
    assert "/dr/scripts/idle.sh" in dockerfile
    assert "/dr/scripts/idle.sh" in railway


def test_executor_image_includes_backup_dependencies() -> None:
    dockerfile = (EXECUTOR_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "aws-cli" in dockerfile
    assert "jq" in dockerfile
    assert "COPY contracts/" in dockerfile
    assert "COPY scripts/" in dockerfile
