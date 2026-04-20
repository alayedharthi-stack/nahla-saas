"""
tests/test_delivery_policy.py
─────────────────────────────
Pin the contract of :func:`backend.services.delivery_policy.resolve_delivery_mode`.

The policy module is the *single source of truth* for "given a step
config + the live customer-service-window state, what wire format do
we send?" Every other call site (the automation engine, the
conversion layer, future preview UIs) must go through it. So if a
regression slips into one of the resolution rules, every cart-recovery
flow breaks at once. These tests pin the table from the module docstring
case-by-case so any future change is loud and intentional.

We deliberately test *behaviour*, not implementation:
  • The "auto" primary picks the smartest legal mode given the inputs.
  • In-window-only modes (interactive, ai_recovery) fall back to
    template when the window has closed.
  • ai_recovery additionally falls back when the AI isn't eligible.
  • Template is always legal — never falls back.
  • Legacy ``delivery_mode`` is honoured when ``primary_mode`` is
    missing, so configs saved before the policy editor shipped keep
    working without a migration.
  • ``fallback_mode == "none"`` is *recorded* but the resolver still
    returns a legal wire format; the engine is responsible for
    skipping the send if the merchant explicitly opted out.
"""
from __future__ import annotations

import os
import sys

# Make the `backend` package importable the same way the rest of the
# suite does — without forcing a conftest.py change for one new file.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest

from services.delivery_policy import (  # noqa: E402  (sys.path tweak above)
    DeliveryDecision,
    resolve_delivery_mode,
)


# ── "auto" primary — the recommended default ─────────────────────────────────

class TestAutoPrimary:
    """The synthetic ``auto`` primary picks the smartest legal mode."""

    def test_auto_picks_ai_recovery_when_window_open_and_ai_eligible(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "auto"},
            window_open=True, ai_eligible=True,
        )
        assert d.mode == "ai_recovery"
        assert d.reason == "auto:window_open+ai_eligible"
        assert d.used_fallback is False

    def test_auto_picks_interactive_when_window_open_no_ai(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "auto"},
            window_open=True, ai_eligible=False,
        )
        assert d.mode == "interactive"
        assert d.reason == "auto:window_open"
        assert d.used_fallback is False

    def test_auto_picks_template_when_window_closed(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "auto"},
            window_open=False, ai_eligible=True,
        )
        assert d.mode == "template"
        assert d.reason == "auto:window_closed"
        assert d.used_fallback is False


# ── Explicit "interactive" primary ───────────────────────────────────────────

class TestInteractivePrimary:
    """Interactive sends are only legal inside the 24h service window."""

    def test_interactive_sent_when_window_open(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive"},
            window_open=True, ai_eligible=False,
        )
        assert d.mode == "interactive"
        assert d.reason == "explicit"
        assert d.used_fallback is False

    def test_interactive_falls_back_to_template_when_window_closed(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive"},
            window_open=False, ai_eligible=True,
        )
        assert d.mode == "template"
        assert d.reason == "interactive_unavailable_window_closed"
        assert d.used_fallback is True
        assert d.primary == "interactive"
        assert d.fallback == "template"


# ── Explicit "ai_recovery" primary ───────────────────────────────────────────

class TestAiRecoveryPrimary:
    """AI recovery needs both the window AND the AI toggle to fire."""

    def test_ai_recovery_sent_when_window_open_and_ai_eligible(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "ai_recovery"},
            window_open=True, ai_eligible=True,
        )
        assert d.mode == "ai_recovery"
        assert d.reason == "explicit"
        assert d.used_fallback is False

    def test_ai_recovery_falls_back_when_window_closed(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "ai_recovery"},
            window_open=False, ai_eligible=True,
        )
        assert d.mode == "template"
        assert d.reason == "ai_recovery_unavailable_window_closed"
        assert d.used_fallback is True

    def test_ai_recovery_falls_back_when_ai_not_eligible(self) -> None:
        # Window IS open, but the merchant disabled AI recovery — we
        # must not return ``error: ai_recovery_disabled`` like the old
        # code did. We must fall back.
        d = resolve_delivery_mode(
            step={"primary_mode": "ai_recovery"},
            window_open=True, ai_eligible=False,
        )
        assert d.mode == "template"
        assert d.reason == "ai_recovery_unavailable_not_eligible"
        assert d.used_fallback is True


# ── Explicit "template" primary ──────────────────────────────────────────────

class TestTemplatePrimary:
    """Template is always legal — no fallback ever needed."""

    @pytest.mark.parametrize("window", [True, False])
    @pytest.mark.parametrize("ai", [True, False])
    def test_template_always_template(self, window: bool, ai: bool) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "template"},
            window_open=window, ai_eligible=ai,
        )
        assert d.mode == "template"
        assert d.reason == "explicit_template"
        assert d.used_fallback is False


# ── Backwards-compat with the legacy ``delivery_mode`` field ────────────────

class TestLegacyDeliveryModeField:
    """Configs saved before the policy editor shipped keep working."""

    def test_legacy_template_is_honoured(self) -> None:
        d = resolve_delivery_mode(
            step={"delivery_mode": "template"},
            window_open=True, ai_eligible=True,
        )
        assert d.mode == "template"

    def test_legacy_interactive_is_honoured_with_window_aware_fallback(self) -> None:
        d = resolve_delivery_mode(
            step={"delivery_mode": "interactive"},
            window_open=False, ai_eligible=False,
        )
        # Same fallback semantics as the new primary_mode field.
        assert d.mode == "template"
        assert d.used_fallback is True

    def test_legacy_ai_recovery_falls_back_when_ineligible(self) -> None:
        # The OLD engine returned ``error: ai_recovery_disabled`` here
        # and dropped the send. The whole point of this rewrite is
        # that we now fall back instead.
        d = resolve_delivery_mode(
            step={"delivery_mode": "ai_recovery"},
            window_open=True, ai_eligible=False,
        )
        assert d.mode == "template"
        assert d.used_fallback is True

    def test_primary_mode_wins_when_both_are_set(self) -> None:
        # If a step somehow has both fields (e.g. mid-rollout),
        # primary_mode is the new source of truth.
        d = resolve_delivery_mode(
            step={"primary_mode": "template", "delivery_mode": "interactive"},
            window_open=True, ai_eligible=True,
        )
        assert d.mode == "template"
        assert d.primary == "template"


# ── Config-level defaults ────────────────────────────────────────────────────

class TestAutomationLevelDefaults:
    """Automation-wide defaults apply when the step doesn't set them."""

    def test_config_primary_mode_is_used_when_step_silent(self) -> None:
        d = resolve_delivery_mode(
            step={},
            config={"primary_mode": "template"},
            window_open=False, ai_eligible=False,
        )
        assert d.mode == "template"
        assert d.primary == "template"

    def test_step_primary_mode_overrides_config(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive"},
            config={"primary_mode": "template"},
            window_open=True, ai_eligible=False,
        )
        assert d.mode == "interactive"


# ── Fallback configuration ───────────────────────────────────────────────────

class TestFallbackMode:
    """The fallback slot is honoured when primary isn't legal."""

    def test_default_fallback_is_template(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive"},  # no fallback_mode
            window_open=False, ai_eligible=False,
        )
        assert d.fallback == "template"
        assert d.mode == "template"

    def test_explicit_fallback_template_works(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive", "fallback_mode": "template"},
            window_open=False, ai_eligible=False,
        )
        assert d.mode == "template"

    def test_unknown_fallback_is_treated_as_template(self) -> None:
        # Defensive: a typo in the merchant config should default to
        # the safe option, not raise.
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive", "fallback_mode": "voice"},
            window_open=False, ai_eligible=True,
        )
        assert d.fallback == "template"
        assert d.mode == "template"


# ── Empty / malformed input ──────────────────────────────────────────────────

class TestDefensiveDefaults:
    """An empty step config should default to the safe smart mode."""

    def test_empty_step_defaults_to_auto(self) -> None:
        d = resolve_delivery_mode(
            step={}, window_open=True, ai_eligible=True,
        )
        # auto + window + ai → ai_recovery, never raises
        assert d.primary == "auto"
        assert d.mode == "ai_recovery"

    def test_unknown_primary_mode_defaults_to_auto(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "voicemail"},
            window_open=False, ai_eligible=False,
        )
        assert d.primary == "auto"
        assert d.mode == "template"

    def test_resolution_never_raises(self) -> None:
        # Sweep a small grid to make sure no permutation throws.
        for primary in ["auto", "template", "interactive", "ai_recovery", "bogus", ""]:
            for window in [True, False]:
                for ai in [True, False]:
                    d = resolve_delivery_mode(
                        step={"primary_mode": primary},
                        window_open=window, ai_eligible=ai,
                    )
                    assert isinstance(d, DeliveryDecision)
                    assert d.mode in {"template", "interactive", "ai_recovery"}


# ── Audit serialisation ──────────────────────────────────────────────────────

class TestAuditPayload:
    """``to_audit()`` exposes everything the metrics/logs need."""

    def test_audit_includes_all_fields(self) -> None:
        d = resolve_delivery_mode(
            step={"primary_mode": "interactive", "fallback_mode": "template"},
            window_open=False, ai_eligible=False,
        )
        audit = d.to_audit()
        for key in ("mode", "reason", "primary", "fallback",
                    "used_fallback", "window_open", "ai_eligible"):
            assert key in audit, f"missing audit key: {key}"
        assert audit["used_fallback"] is True
        assert audit["mode"] == "template"
        assert audit["primary"] == "interactive"
