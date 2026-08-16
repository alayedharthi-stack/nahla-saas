"""Idempotent tenant-config hygiene — obsolete allowlist + platform_type SoT."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from brain_replay_fixtures import BrainReplaySnapshot, make_brain_replay_db_and_world  # noqa: E402
from core.tenant import DEFAULT_AI, DEFAULT_STORE, get_or_create_settings, merge_ai_defaults  # noqa: E402
from core.tenant_config_hygiene import (  # noqa: E402
    OBSOLETE_AI_KEYS,
    apply_tenant_settings_hygiene,
    canonical_platform_type,
    normalize_all_tenant_settings,
    strip_obsolete_ai_keys,
)
from models import Integration, TenantSettings  # noqa: E402


def test_canonical_defaults_have_no_allowlist_key() -> None:
    assert "persona_composer_allowlist_tenants" not in DEFAULT_AI
    assert DEFAULT_STORE["platform_type"] == "custom"


def test_merge_ai_defaults_strips_legacy_key() -> None:
    merged = merge_ai_defaults(
        {
            "persona_composer_enabled": True,
            "persona_composer_allowlist_tenants": [33, 1],
            "_kb_backup_v1": "merchant backup",
            "store_ai_mode": "test",
        }
    )
    assert "persona_composer_allowlist_tenants" not in merged
    assert merged["persona_composer_enabled"] is True
    assert merged["_kb_backup_v1"] == "merchant backup"
    assert merged["store_ai_mode"] == "test"


def test_strip_is_idempotent() -> None:
    first, changed = strip_obsolete_ai_keys(
        {"persona_composer_allowlist_tenants": [9], "store_ai_mode": "on"}
    )
    assert changed is True
    second, changed_again = strip_obsolete_ai_keys(first)
    assert changed_again is False
    assert "persona_composer_allowlist_tenants" not in second


def test_platform_type_without_connection_becomes_custom() -> None:
    assert canonical_platform_type({"platform_type": "salla"}, None) == "custom"
    assert canonical_platform_type({"platform_type": "zid"}, None) == "custom"
    assert canonical_platform_type({"platform_type": "custom"}, None) == "custom"


def test_platform_type_follows_enabled_integration() -> None:
    assert canonical_platform_type({"platform_type": "custom"}, "salla") == "salla"
    assert canonical_platform_type({"platform_type": "salla"}, "salla") == "salla"


def test_stale_salla_label_without_integration_is_persisted_custom() -> None:
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ts.ai_settings = {
        **dict(ts.ai_settings or {}),
        "persona_composer_allowlist_tenants": [33],
        "persona_composer_enabled": True,
        "store_ai_mode": "test",
        "_kb_backup_v1": "keep-me",
    }
    ts.store_settings = {
        "platform_type": "salla",
        "salla_access_token": "",
        "store_name": "متجر تجريبي عام",
    }
    db.add(ts)
    db.commit()

    report = apply_tenant_settings_hygiene(db, ts)
    db.commit()
    db.refresh(ts)

    assert report["changed"] is True
    assert report["persona_allowlist_removed"] is True
    assert report["platform_after"] == "custom"
    assert "persona_composer_allowlist_tenants" not in (ts.ai_settings or {})
    assert (ts.store_settings or {}).get("platform_type") == "custom"
    assert (ts.ai_settings or {}).get("persona_composer_enabled") is True
    assert (ts.ai_settings or {}).get("store_ai_mode") == "test"
    assert (ts.ai_settings or {}).get("_kb_backup_v1") == "keep-me"

    again = apply_tenant_settings_hygiene(db, ts)
    assert again["changed"] is False


def test_enabled_salla_integration_keeps_salla_label() -> None:
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ts.store_settings = {"platform_type": "custom", "salla_access_token": ""}
    db.add(
        Integration(
            provider="salla",
            external_store_id="hygiene-salla-store",
            tenant_id=world.tenant.id,
            enabled=True,
            config={"platform": "salla"},
        )
    )
    db.add(ts)
    db.commit()

    report = apply_tenant_settings_hygiene(db, ts)
    db.commit()
    db.refresh(ts)
    assert report["platform_after"] == "salla"
    assert (ts.store_settings or {}).get("platform_type") == "salla"


def test_normalize_all_is_platform_wide_and_idempotent() -> None:
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ts.ai_settings = {"persona_composer_allowlist_tenants": [1, 99]}
    ts.store_settings = {"platform_type": "salla", "salla_access_token": ""}
    db.add(ts)
    db.commit()

    first = normalize_all_tenant_settings(db)
    db.commit()
    assert first["scanned"] >= 1
    assert first["changed"] >= 1
    second = normalize_all_tenant_settings(db)
    assert second["changed"] == 0
    db.refresh(ts)
    assert "persona_composer_allowlist_tenants" not in (ts.ai_settings or {})


def test_get_or_create_settings_persists_hygiene() -> None:
    db, world = make_brain_replay_db_and_world(
        BrainReplaySnapshot(tenant_name="متجر تجريبي عام")
    )
    ts = db.query(TenantSettings).filter_by(tenant_id=world.tenant.id).one()
    ts.ai_settings = {"persona_composer_allowlist_tenants": [7]}
    ts.store_settings = {"platform_type": "shopify", "shopify_access_token": ""}
    db.add(ts)
    db.commit()

    loaded = get_or_create_settings(db, world.tenant.id)
    db.commit()
    db.refresh(loaded)
    assert "persona_composer_allowlist_tenants" not in (loaded.ai_settings or {})
    assert (loaded.store_settings or {}).get("platform_type") == "custom"


def test_obsolete_key_constant_is_closed() -> None:
    assert OBSOLETE_AI_KEYS == frozenset({"persona_composer_allowlist_tenants"})


def test_runtime_has_zero_semantic_reads_of_legacy_allowlist() -> None:
    """Gating/ownership must not read the retired key. Strip/pop only."""
    from pathlib import Path

    key = "persona_composer_allowlist_tenants"
    repo = Path(__file__).resolve().parents[2]
    roots = [
        repo / "backend" / "core",
        repo / "backend" / "modules",
        repo / "backend" / "routers",
        repo / "backend" / "services",
    ]
    strip_files = {"tenant.py", "tenant_config_hygiene.py"}
    hits: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if key not in text:
                continue
            rel = str(path.relative_to(repo)).replace("\\", "/")
            get_read = f'.get("{key}"' in text or f".get('{key}'" in text
            sub_read = f'["{key}"]' in text or f"['{key}']" in text
            if path.name in strip_files:
                if get_read or sub_read:
                    hits.append(f"{rel}: unexpected get/subscript of retired key")
                continue
            if path.name == "flags.py" and "persona" in rel:
                if get_read or sub_read:
                    hits.append(f"{rel}: unexpected get/subscript of retired key")
                continue
            hits.append(rel)
    assert hits == [], "retired allowlist must not remain in runtime: " + "; ".join(hits)
