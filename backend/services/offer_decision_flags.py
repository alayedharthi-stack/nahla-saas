"""
services/offer_decision_flags.py
────────────────────────────────
Single source of truth for the per-tenant rollout mode of the
**OfferDecisionService**.

Why this module exists
──────────────────────
Before this module each surface (chat, automation engine, customer
intelligence segment-change) read its own copy of the
`offer_decision_service` flag. The chat path additionally had a built-in
"default-advisory" behaviour, while automation and segment-change went
straight from OFF to full ENFORCE the moment the flag flipped on. That
made a safe rollout impossible — you couldn't get telemetry on the
critical-path surfaces without putting the service on the critical path
at the same time.

This module introduces a second, optional flag that lets staff request
**advisory mode** (compute the decision, write a ledger row, but let the
existing legacy code path produce the actual artefact) consistently
across all three surfaces. The legacy decision flag keeps its existing
meaning so live tenants don't change behaviour when this module ships.

Truth table
───────────
+-----------------------------+-----------------------------------+----------+
|  offer_decision_service     |  offer_decision_service_advisory  |  Mode    |
+-----------------------------+-----------------------------------+----------+
|  any                        |  true                             | ADVISORY |
|  true                       |  false / unset                    | ENFORCE  |
|  false / unset              |  false / unset                    | OFF      |
+-----------------------------+-----------------------------------+----------+

Notes
─────
• ADVISORY always wins. This makes the advisory flag a safety brake:
  flip it on at any time to downgrade an enforce-mode tenant back to
  shadow without un-setting the main flag.
• OFF preserves the legacy zero-ledger behaviour for automation and
  segment-change. The chat path keeps its independent default-advisory
  behaviour (it has always written ledger rows for telemetry parity);
  the chat surface treats OFF and ADVISORY identically — the only
  observable difference for chat is OFF/ADVISORY vs ENFORCE.
• Reading the flags never raises. A misconfigured tenant or a missing
  TenantSettings row degrades to OFF.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


# ── Public flag keys ────────────────────────────────────────────────────
#
# Kept here so callers (admin UI, tests, scripts) reference one constant
# instead of stringly-typed flag names. The DB column is unchanged:
#   tenant_settings.metadata -> 'tenant_features' -> {<flag_key>: bool}

FLAG_SERVICE  = "offer_decision_service"           # legacy "enforce" flag
FLAG_ADVISORY = "offer_decision_service_advisory"  # new shadow-mode flag


class DecisionMode(str, Enum):
    """Per-tenant rollout mode for OfferDecisionService."""

    OFF      = "off"        # legacy code is authoritative; no ledger writes
                            # from automation/segment-change. (Chat keeps
                            # its built-in advisory ledger writes — see
                            # adapter._execute_suggest_coupon.)

    ADVISORY = "advisory"   # decision is computed and persisted to the
                            # ledger, BUT the existing legacy code path
                            # still produces the artefact returned to
                            # callers. The artefact is back-stamped with
                            # `decision_id` so attribution still works.

    ENFORCE  = "enforce"    # OfferDecisionService is the source of truth
                            # for source / value / validity. The legacy
                            # code path is bypassed entirely.


# ── Public API ──────────────────────────────────────────────────────────

def tenant_decision_mode(db: Session, tenant_id: int) -> DecisionMode:
    """Resolve the rollout mode for one tenant.

    Implements the truth table at the top of the module. Never raises —
    on any read failure returns ``OFF`` so the legacy code path keeps
    serving traffic.
    """
    flags = _read_tenant_flags(db, tenant_id)
    if flags.get(FLAG_ADVISORY):
        return DecisionMode.ADVISORY
    if flags.get(FLAG_SERVICE):
        return DecisionMode.ENFORCE
    return DecisionMode.OFF


def is_enforce(db: Session, tenant_id: int) -> bool:
    """Convenience check used by callers that only care whether the
    decision service is *authoritative*. Equivalent to
    ``tenant_decision_mode(...) is ENFORCE`` but cheaper to read at call
    sites that don't need the tri-state."""
    return tenant_decision_mode(db, tenant_id) is DecisionMode.ENFORCE


def is_advisory(db: Session, tenant_id: int) -> bool:
    """Convenience check for the shadow-mode branch."""
    return tenant_decision_mode(db, tenant_id) is DecisionMode.ADVISORY


def stamp_decision_id_on_coupon(
    db: Session,
    coupon: Any,
    decision: Any,
    *,
    mode_label: str,
) -> None:
    """Back-stamp a coupon issued by a legacy path with the
    ``decision_id`` of the advisory decision that was logged for the
    same surface, so :class:`OfferAttributionService` can still close
    the loop on redemption.

    No-op when either argument is missing or when the coupon is already
    stamped with the same id. Failures are swallowed and logged at
    DEBUG — never escalates to the caller.

    Parameters
    ----------
    db          : open Session, will be flushed (but not committed) on success.
    coupon      : a `Coupon` ORM row produced by the legacy path.
    decision    : an `OfferDecision` returned from `decide()`.
    mode_label  : free-form short string written into
                  `coupon.extra_metadata.decision_mode` so analytics can
                  separate advisory-stamped coupons from enforce-stamped
                  ones (typical values: "advisory", "advisory_chat",
                  "advisory_automation", "advisory_segment_change").
    """
    if coupon is None or decision is None:
        return
    decision_id = getattr(decision, "decision_id", None)
    if not decision_id:
        return
    try:
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415
        from services.offer_decision_service import _link_coupon_to_ledger  # noqa: PLC0415

        meta = dict(getattr(coupon, "extra_metadata", None) or {})
        if meta.get("decision_id") == decision_id:
            return
        meta["decision_id"] = decision_id
        meta.setdefault("decision_mode", mode_label)
        coupon.extra_metadata = meta
        flag_modified(coupon, "extra_metadata")
        try:
            db.flush()
        except Exception as exc:  # pragma: no cover — defensive flush
            logger.debug("[offer_decision_flags] flush after stamp failed: %s", exc)
        coupon_id = getattr(coupon, "id", None)
        if coupon_id is not None:
            _link_coupon_to_ledger(db, decision_id, coupon_id)
    except Exception as exc:  # pragma: no cover — never block the caller
        logger.debug("[offer_decision_flags] stamp_decision_id_on_coupon failed: %s", exc)


# ── Internals ───────────────────────────────────────────────────────────

def _read_tenant_flags(db: Session, tenant_id: Optional[int]) -> Dict[str, Any]:
    """Read the ``tenant_features`` blob for one tenant. Returns ``{}``
    on any failure or when the row is missing."""
    if tenant_id is None:
        return {}
    try:
        from models import TenantSettings  # noqa: PLC0415

        ts = db.query(TenantSettings).filter_by(tenant_id=int(tenant_id)).first()
        if ts is None:
            return {}
        meta = dict(getattr(ts, "extra_metadata", None) or {})
        flags = meta.get("tenant_features") or {}
        return dict(flags) if isinstance(flags, dict) else {}
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("[offer_decision_flags] tenant_features read failed: %s", exc)
        return {}
