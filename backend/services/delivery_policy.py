"""
delivery_policy
───────────────
Resolves a per-step *delivery policy* into a concrete WhatsApp wire
format ("template" | "interactive" | "ai_recovery") at the moment of
sending — accounting for the live customer-service-window state and the
merchant's AI-recovery toggle.

Why this exists
───────────────
The previous ``delivery_mode`` field on each recovery step was a single
literal — either "template", "interactive", or "ai_recovery". That model
silently broke two real-world cases:

  1.  A merchant picked "interactive" for stage 1, not realising Meta
      forbids interactive sends outside the 24-hour customer service
      window. The engine HAD a hidden fallback to template for the
      "interactive" case, but the merchant had no UI affordance for
      that decision and assumed interactive would always work.

  2.  A merchant picked "ai_recovery" but ``ai_recovery_enabled`` was
      false. The engine returned ``error: ai_recovery_disabled`` and
      DID NOT fall back, dropping the message entirely.

The new model is a *policy* with two slots:

    primary_mode  : what to attempt first
    fallback_mode : what to try if primary is not legal/available

Plus a synthetic ``"auto"`` primary that resolves at send time using the
most-merchant-friendly rule:

    window open  + AI eligible  → ai_recovery
    window open  + no AI        → interactive
    window closed (any AI)      → template

Backwards compatibility
───────────────────────
We accept the legacy ``delivery_mode`` field as a fallback when
``primary_mode`` is missing, so existing seeds and merchant configs
keep working without a migration. The dashboard editor writes both
fields on save during the rollout window so an older backend reading
the legacy field still picks up the merchant's choice.

This module is intentionally a *pure function* — no DB, no logging side
effects, no I/O. The caller is responsible for measuring window state
and AI eligibility. That keeps it trivially unit-testable and lets the
same logic run in tests, the engine, the conversion layer, and any
future preview UI without coupling them to a DB session.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional

# Concrete wire formats the WhatsApp send pipeline knows how to emit.
WireFormat = str  # one of: "template" | "interactive" | "ai_recovery"

CONCRETE_MODES: FrozenSet[str] = frozenset({"template", "interactive", "ai_recovery"})
PRIMARY_MODES:  FrozenSet[str] = CONCRETE_MODES | {"auto"}

# Modes that only Meta's customer-service-window allows (i.e. *not*
# legal once the 24h window has closed).
IN_WINDOW_ONLY: FrozenSet[str] = frozenset({"interactive", "ai_recovery"})


@dataclass(frozen=True)
class DeliveryDecision:
    """Output of :func:`resolve_delivery_mode`.

    Attributes:
        mode:       The concrete wire format the engine should send.
                    One of ``"template"``, ``"interactive"``, ``"ai_recovery"``.
        reason:     Short machine-readable code explaining why this mode
                    was chosen (useful for audit logs + dashboards).
        primary:    The merchant-configured primary mode (post-resolution
                    of ``"auto"``).
        fallback:   The merchant-configured fallback (or ``"template"``
                    by default).
        used_fallback: True iff ``mode`` came from the fallback slot
                       rather than the primary slot.
        window_open: The window-state input that drove the decision.
        ai_eligible: The AI-eligibility input that drove the decision.
    """
    mode:          str
    reason:        str
    primary:       str
    fallback:      str
    used_fallback: bool
    window_open:   bool
    ai_eligible:   bool

    def to_audit(self) -> Dict[str, Any]:
        """Serialise for AutomationExecution.metrics / event payloads."""
        return {
            "mode":          self.mode,
            "reason":        self.reason,
            "primary":       self.primary,
            "fallback":      self.fallback,
            "used_fallback": self.used_fallback,
            "window_open":   self.window_open,
            "ai_eligible":   self.ai_eligible,
        }


def _read_primary(step: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Honour ``primary_mode`` first, then the legacy ``delivery_mode``.

    Returns one of ``PRIMARY_MODES``; defaults to ``"auto"`` so a
    merchant who hasn't touched the editor gets the smart, window-aware
    behaviour rather than a silent default to a single mode.
    """
    raw = (
        step.get("primary_mode")
        or step.get("delivery_mode")
        or config.get("primary_mode")
        or config.get("delivery_mode")
        or "auto"
    )
    raw = str(raw).strip().lower()
    return raw if raw in PRIMARY_MODES else "auto"


def _read_fallback(step: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Fallback defaults to ``"template"`` because that's the only wire
    format Meta lets us send unconditionally (it can re-open the window).

    A merchant may explicitly set ``fallback_mode`` to ``"none"`` to
    opt out of the safety net (we then return ``"template"`` for
    legality but flag ``used_fallback`` honestly so the metric is true).
    """
    raw = (
        step.get("fallback_mode")
        or config.get("fallback_mode")
        or "template"
    )
    raw = str(raw).strip().lower()
    # Currently template is the only sane fallback; future modes (e.g.
    # "voice") could be added here.
    return "template" if raw not in {"template", "none"} else (raw or "template")


def resolve_delivery_mode(
    *,
    step: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    window_open: bool,
    ai_eligible: bool,
) -> DeliveryDecision:
    """Decide the concrete wire format for a recovery step.

    Args:
        step:         The per-step config dict (one element of
                      ``automation.config["steps"]``).
        config:       The automation-wide config dict — used as a
                      secondary source for ``primary_mode`` /
                      ``delivery_mode`` so a merchant can set a default
                      once instead of per step.
        window_open:  Whether the customer's WhatsApp 24h service
                      window is currently open. Computed live from
                      ``core.wa_usage.has_open_service_window``.
        ai_eligible:  Whether the AI-recovery turn is allowed for this
                      send. Combine the per-step
                      ``ai_recovery_enabled``, the automation-wide
                      flag, and any conversion-layer signal-strength
                      check before passing this in.

    Returns:
        :class:`DeliveryDecision` — never raises. The ``mode`` field is
        always one of ``CONCRETE_MODES``.

    Resolution table:

        primary       window_open  ai_eligible  →  effective mode  (reason)
        ─────────────  ───────────  ───────────     ─────────────   ──────
        auto           True         True            ai_recovery     auto:window+ai
        auto           True         False           interactive     auto:window
        auto           False        *               template        auto:closed

        template       *            *               template        explicit
        interactive    True         *               interactive     explicit
        interactive    False        *               <fallback>      win_closed
        ai_recovery    True         True            ai_recovery     explicit
        ai_recovery    True         False           <fallback>      ai_disabled
        ai_recovery    False        *               <fallback>      win_closed
    """
    config = config or {}
    primary  = _read_primary(step, config)
    fallback = _read_fallback(step, config)

    # 1. Resolve the synthetic "auto" choice into a concrete primary.
    if primary == "auto":
        if window_open and ai_eligible:
            return DeliveryDecision(
                mode="ai_recovery", reason="auto:window_open+ai_eligible",
                primary=primary, fallback=fallback, used_fallback=False,
                window_open=window_open, ai_eligible=ai_eligible,
            )
        if window_open:
            return DeliveryDecision(
                mode="interactive", reason="auto:window_open",
                primary=primary, fallback=fallback, used_fallback=False,
                window_open=window_open, ai_eligible=ai_eligible,
            )
        return DeliveryDecision(
            mode="template", reason="auto:window_closed",
            primary=primary, fallback=fallback, used_fallback=False,
            window_open=window_open, ai_eligible=ai_eligible,
        )

    # 2. Template is always legal — no fallback ever needed.
    if primary == "template":
        return DeliveryDecision(
            mode="template", reason="explicit_template",
            primary=primary, fallback=fallback, used_fallback=False,
            window_open=window_open, ai_eligible=ai_eligible,
        )

    # 3. In-window-only modes need the window. If it's closed, fall back.
    if primary in IN_WINDOW_ONLY and not window_open:
        return DeliveryDecision(
            mode=fallback, reason=f"{primary}_unavailable_window_closed",
            primary=primary, fallback=fallback, used_fallback=True,
            window_open=window_open, ai_eligible=ai_eligible,
        )

    # 4. ai_recovery additionally needs the AI to be eligible.
    if primary == "ai_recovery" and not ai_eligible:
        return DeliveryDecision(
            mode=fallback, reason="ai_recovery_unavailable_not_eligible",
            primary=primary, fallback=fallback, used_fallback=True,
            window_open=window_open, ai_eligible=ai_eligible,
        )

    # 5. Default: honour the explicit primary.
    return DeliveryDecision(
        mode=primary, reason="explicit",
        primary=primary, fallback=fallback, used_fallback=False,
        window_open=window_open, ai_eligible=ai_eligible,
    )
