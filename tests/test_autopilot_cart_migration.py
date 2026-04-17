"""
tests/test_autopilot_cart_migration.py
───────────────────────────────────────
Backward-compat for the abandoned-cart autopilot shape.

The dashboard used to expose `reminder_24h` + `coupon_48h`. The new
3-stage workflow uses `reminder_6h` + `coupon_24h`. Merchants who
saved the old shape get an in-memory rewrite on every read so:

  • their previous "send a coupon" intent is preserved
    (legacy coupon_48h=True → new coupon_24h=True)
  • the legacy keys are stripped from the response so the dashboard
    never renders the retired toggles
  • new keys default to the workflow defaults when missing
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from routers.automations import (  # noqa: E402
    DEFAULT_AUTOPILOT,
    _migrate_legacy_abandoned_cart_settings,
)


def test_default_shape_pins_three_stage_workflow() -> None:
    sub = DEFAULT_AUTOPILOT["abandoned_cart"]
    assert sub["enabled"] is True
    assert sub["reminder_30min"] is True
    assert sub["reminder_6h"] is True
    assert sub["coupon_24h"] is False
    # Legacy keys must NOT be in the default — otherwise they'd leak
    # back through `_get_autopilot_settings` and the dashboard would
    # render the retired toggles again.
    assert "reminder_24h" not in sub
    assert "coupon_48h" not in sub


def test_legacy_coupon_intent_preserved() -> None:
    """A merchant who had `coupon_48h=True` on the old shape must
    keep getting a coupon on the new shape — just at the new 24h
    boundary instead of 48h."""
    legacy = {
        "enabled":        True,
        "reminder_30min": True,
        "reminder_24h":   True,
        "coupon_48h":     True,
        "coupon_code":    "WELCOME10",
    }
    out = _migrate_legacy_abandoned_cart_settings(legacy)
    assert out["coupon_24h"] is True
    assert out["reminder_6h"] is True
    assert out["coupon_code"] == "WELCOME10"
    # Old keys must be stripped.
    assert "coupon_48h" not in out
    assert "reminder_24h" not in out


def test_legacy_disabled_stays_disabled() -> None:
    legacy = {
        "enabled":        True,
        "reminder_30min": True,
        "reminder_24h":   False,
        "coupon_48h":     False,
    }
    out = _migrate_legacy_abandoned_cart_settings(legacy)
    assert out["reminder_6h"] is False
    assert out["coupon_24h"] is False


def test_already_migrated_shape_passes_through() -> None:
    """A merchant who saved the new shape must NOT get clobbered."""
    new_shape = {
        "enabled":        False,
        "reminder_30min": True,
        "reminder_6h":    False,
        "coupon_24h":     True,
        "coupon_code":    "FALL10",
    }
    out = _migrate_legacy_abandoned_cart_settings(new_shape)
    assert out["reminder_6h"] is False
    assert out["coupon_24h"] is True
    assert out["enabled"] is False
    assert out["coupon_code"] == "FALL10"


def test_empty_input_returns_defaults() -> None:
    out = _migrate_legacy_abandoned_cart_settings({})
    assert out == DEFAULT_AUTOPILOT["abandoned_cart"]


def test_non_dict_input_falls_back_to_defaults() -> None:
    """Defensive: a corrupt row stored as `None` or a list shouldn't
    crash the dashboard read — it should look like a fresh tenant."""
    assert _migrate_legacy_abandoned_cart_settings(None) == DEFAULT_AUTOPILOT["abandoned_cart"]  # type: ignore[arg-type]
    assert _migrate_legacy_abandoned_cart_settings([]) == DEFAULT_AUTOPILOT["abandoned_cart"]  # type: ignore[arg-type]
