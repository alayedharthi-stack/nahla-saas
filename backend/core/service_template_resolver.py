"""
core/service_template_resolver.py
─────────────────────────────────
Service-aware template resolution layer.

KEY CONCEPTS
────────────
1. **Service** (`service_key`):  the business purpose (e.g. `cart_recovery`,
   `cod_confirmation`).  The service is the *stable identity* — templates
   are just the *current binding* that can be swapped at any time.

2. **Step** (`step_number`):  within a service a multi-step sequence may
   exist (e.g. cart recovery has steps 1-4).

3. **Active Template invariant**:  for every combination of
   `(tenant_id, service_key, step_number)` at most ONE template may be
   active (`is_active=True`) and visible (`is_hidden=False`) at any time.
   The DB enforces this with a partial unique index.

4. **Session-window rule**:
   - Inside the 24h WhatsApp service window → AI / interactive replies.
   - Outside the window → only a Meta-APPROVED template via this resolver.

PUBLIC API
──────────
  ensure_single_active(db, tenant_id, service_key, step_number, new_active_id)
      Deactivates any other template for the same slot, returns the old one.

  resolve_active_template(db, tenant_id, service_key, step_number)
      Returns the single active+visible+APPROVED template for a slot, or None.
      Strict — used by callers that must NOT auto-bind anything.

  resolve_template_for_send(db, tenant_id, service_key, step_number,
                            *, fallback_template_name=None)
      Send-flow tolerant resolver. Walks a documented fallback chain
      and AUTO-BINDS the first APPROVED template that plausibly serves
      the slot. Used by `automation_engine` so cart-recovery sends
      self-heal instead of failing with `template_not_approved` when
      the merchant's APPROVED templates exist but are unbound.

  list_alternatives(db, tenant_id, service_key, step_number)
      Returns all templates for the same slot (active first, then inactive).
"""
from __future__ import annotations

import logging
from typing import Optional, List

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def ensure_single_active(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
    new_active_id: int,
) -> Optional[int]:
    """
    Activate *new_active_id* and deactivate every other template that shares
    the same (tenant_id, service_key, step_number).

    Returns the id of the previously-active template (or None).
    Must be called inside an existing transaction — caller commits.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    prev_active_id: Optional[int] = None

    others = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.is_active   == True,   # noqa: E712
            WhatsAppTemplate.id          != new_active_id,
        )
        .all()
    )
    for tpl in others:
        prev_active_id = prev_active_id or tpl.id
        tpl.is_active = False
        logger.info(
            "[ServiceResolver] Deactivated template id=%s name=%s "
            "(slot: tenant=%s service=%s step=%s) — replaced by id=%s",
            tpl.id, tpl.name, tenant_id, service_key, step_number,
            new_active_id,
        )

    target = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.id        == new_active_id,
            WhatsAppTemplate.tenant_id == tenant_id,
        )
        .first()
    )
    if target:
        target.is_active = True
        target.is_hidden = False

    return prev_active_id


def resolve_active_template(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
) -> Optional["WhatsAppTemplate"]:  # noqa: F821
    """
    Return the single active, visible, APPROVED template for a service slot.

    Used by the automation engine when the 24h window is CLOSED and a
    template message is the only legal send mechanism.

    Returns None when no qualifying template exists (the caller should
    log this and skip the send rather than crash).
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    return (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.is_active   == True,   # noqa: E712
            WhatsAppTemplate.is_hidden   == False,  # noqa: E712
            WhatsAppTemplate.status      == "APPROVED",
        )
        .first()
    )


# ── Smart fallback resolution (used by the automation engine at send time) ──

def _library_keys_for_slot(service_key: str, step_number: int) -> List[str]:
    """Return every Nahla-library `key` that targets this service slot.

    Used to auto-bind an APPROVED template that was imported from the
    library but somehow lost its `service_key` / `step_number`
    (e.g. a `/templates/sync` after the merchant created the template
    directly in Meta Business Manager and bypassed the import flow)."""
    try:
        from services.whatsapp_templates.nahla_templates import NAHLA_TEMPLATES  # noqa: PLC0415
    except Exception:
        return []
    return [
        t["key"] for t in NAHLA_TEMPLATES
        if t.get("service_key") == service_key
        and t.get("step_number") == step_number
    ]


def _autobind(
    db: Session,
    tpl: "WhatsAppTemplate",  # noqa: F821
    *,
    tenant_id: int,
    service_key: str,
    step_number: int,
    reason: str,
) -> None:
    """Stamp `service_key` / `step_number` / `is_active=True` on a
    template that the smart resolver matched via name / source-key, so
    every subsequent send hits the strict path with zero ambiguity.

    The caller commits the surrounding transaction; this function only
    flushes so the `ensure_single_active` invariant query sees the new
    binding."""
    changed = False
    if not getattr(tpl, "service_key", None):
        tpl.service_key = service_key
        changed = True
    if getattr(tpl, "step_number", None) != step_number:
        tpl.step_number = step_number
        changed = True
    if not getattr(tpl, "is_active", False):
        tpl.is_active = True
        changed = True
    if getattr(tpl, "is_hidden", False):
        tpl.is_hidden = False
        changed = True
    if changed:
        try:
            db.flush()
            ensure_single_active(db, tenant_id, service_key, step_number, tpl.id)
            logger.info(
                "[ServiceResolver] AUTO-BIND tenant=%s service=%s step=%s "
                "tpl_id=%s name=%s reason=%s",
                tenant_id, service_key, step_number, tpl.id, tpl.name, reason,
            )
        except Exception as exc:
            logger.warning(
                "[ServiceResolver] auto-bind failed tenant=%s tpl_id=%s: %s",
                tenant_id, tpl.id, exc,
            )


def resolve_template_for_send(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: int,
    *,
    fallback_template_name: Optional[str] = None,
) -> Optional["WhatsAppTemplate"]:  # noqa: F821
    """Same intent as `resolve_active_template` but **send-flow tolerant**.

    The strict resolver has been correct for years — but in production
    we hit the failure mode where a merchant has an APPROVED template
    that is simply not bound to a service slot. Three real-world causes:

      1. Merchant created the template directly in Meta Business
         Manager. ``/templates/sync`` ingested it into a fresh row
         without `service_key` / `step_number`.
      2. A previous import row got hidden / deactivated and a new row
         arrived from sync without inheriting the binding.
      3. Sync after import: the original row's ``meta_template_id`` was
         still ``nahla_draft_*`` because the submit step crashed mid-way,
         so the sync created a parallel APPROVED row that was unbound.

    All three cases left the merchant with the right templates approved
    but the cart-recovery automation failing with
    `template_not_approved`. This resolver walks a documented chain
    that ends in **auto-binding** so the issue self-heals on the very
    next inbound cart event.

    Resolution order (first match wins):

      a. Strict: active + visible + APPROVED + matching service_key + step_number.
      b. APPROVED + matching service_key + step_number, ignoring
         is_active / is_hidden flags. Auto-promotes to active.
      c. APPROVED + matching `nahla_source_key` for one of the library
         templates that target this slot. Auto-binds & activates.
      d. APPROVED + name == ``fallback_template_name`` (the legacy
         config-level template name). Auto-binds & activates.
      e. APPROVED + matching service_key (any step_number). Auto-binds
         to the requested step; better than refusing to send.

    Returns ``None`` only when the merchant truly has no APPROVED
    template that could plausibly serve the slot — at which point the
    automation engine surfaces a precise, actionable error."""
    from models import WhatsAppTemplate  # noqa: PLC0415

    # (a) strict
    tpl = resolve_active_template(db, tenant_id, service_key, step_number)
    if tpl:
        return tpl

    # (b) drop is_active / is_hidden
    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.step_number == step_number,
            WhatsAppTemplate.status      == "APPROVED",
        )
        .order_by(WhatsAppTemplate.updated_at.desc())
        .first()
    )
    if tpl:
        _autobind(
            db, tpl, tenant_id=tenant_id, service_key=service_key,
            step_number=step_number, reason="strict_match_inactive",
        )
        return tpl

    # (c) by nahla_source_key for any library template targeting this slot
    library_keys = _library_keys_for_slot(service_key, step_number)
    if library_keys:
        tpl = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id        == tenant_id,
                WhatsAppTemplate.nahla_source_key.in_(library_keys),
                WhatsAppTemplate.status           == "APPROVED",
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .first()
        )
        if tpl:
            _autobind(
                db, tpl, tenant_id=tenant_id, service_key=service_key,
                step_number=step_number,
                reason=f"nahla_source_key={tpl.nahla_source_key}",
            )
            return tpl

    # (d) by config-level template_name (legacy automation seed path)
    if fallback_template_name:
        tpl = (
            db.query(WhatsAppTemplate)
            .filter(
                WhatsAppTemplate.tenant_id == tenant_id,
                WhatsAppTemplate.name      == fallback_template_name,
                WhatsAppTemplate.status    == "APPROVED",
            )
            .order_by(WhatsAppTemplate.updated_at.desc())
            .first()
        )
        if tpl:
            _autobind(
                db, tpl, tenant_id=tenant_id, service_key=service_key,
                step_number=step_number,
                reason=f"config_template_name={fallback_template_name}",
            )
            return tpl

    # (e) any APPROVED template on the same service_key (any step). Useful
    # when the merchant only has ONE recovery template approved and we
    # need to use it across all stages rather than failing every send.
    tpl = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.status      == "APPROVED",
        )
        .order_by(
            WhatsAppTemplate.is_active.desc(),
            WhatsAppTemplate.updated_at.desc(),
        )
        .first()
    )
    if tpl:
        _autobind(
            db, tpl, tenant_id=tenant_id, service_key=service_key,
            step_number=step_number,
            reason=f"service_key_any_step (was step={tpl.step_number})",
        )
        return tpl

    return None


def list_alternatives(
    db: Session,
    tenant_id: int,
    service_key: str,
    step_number: Optional[int] = None,
) -> List["WhatsAppTemplate"]:  # noqa: F821
    """
    Return all templates for a given service slot, active first.

    Useful for the frontend to show alternatives the merchant can swap in.
    """
    from models import WhatsAppTemplate  # noqa: PLC0415

    q = (
        db.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.tenant_id   == tenant_id,
            WhatsAppTemplate.service_key == service_key,
            WhatsAppTemplate.is_hidden   == False,  # noqa: E712
        )
    )
    if step_number is not None:
        q = q.filter(WhatsAppTemplate.step_number == step_number)

    return q.order_by(
        WhatsAppTemplate.is_active.desc(),
        WhatsAppTemplate.updated_at.desc(),
    ).all()
