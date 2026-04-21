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
