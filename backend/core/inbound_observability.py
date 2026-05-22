"""
core/inbound_observability.py
─────────────────────────────
Recorder for **pre-brain** inbound failures so the owner dashboard at
"مراقبة جودة الذكاء" (``/admin/ai-quality``) doesn't keep showing
all-zeros while production is genuinely losing messages.

What this fixes
───────────────
The brain pipeline (``modules.ai.brain.pipeline``) already calls
``core.ai_quality_events.persist_alignment_mismatch`` whenever
``check_alignment`` flags a reply that doesn't match the inbound intent.
That covers the case *the AI answered but answered the wrong question*.

It does NOT cover the case *the AI never ran because we dropped the
message before dispatch*. Production showed those drops are the bulk of
the silent failures merchants notice (Tenant 33, May 22 2026):

  * unsupported message types (sticker / reaction / location / contacts)
    → ``routers/whatsapp_webhook.py:2661``
    ``[TRACE][4/6] INBOUND_IGNORED_UNSUPPORTED``
  * empty text after media normalize with no fallback reply
    → ``routers/whatsapp_webhook.py:2716``
    ``[TRACE][4/6] INBOUND_IGNORED_EMPTY_TEXT``
  * 360dialog webhook unrouted (5 sub-reasons)
    → ``routers/whatsapp_webhook.py:1088-1330``
    ``[UNROUTED_D360_WEBHOOK]``
  * pre-brain handoff branch that returns without saving the inbound
    → ``routers/whatsapp_webhook.py:3686``
  * dispatcher exception thrown between conversation_create and the
    canonical ``StateManager.save_message`` insert
    → ``routers/whatsapp_webhook.py:7056``

This module gives the wiring above ONE function each:
``record_inbound_drop`` and ``record_webhook_unrouted``. They append a
row to ``ai_quality_events`` with ``category != 'ai_mismatch'`` so the
owner dashboard's per-tab queries find them without affecting the
existing AI-mismatch tab.

Design constraints
──────────────────
* **Exception-safe.** A failure here must NEVER break a customer turn,
  fail a webhook 200-ack, or surface to the merchant. Every public
  function returns ``Optional[int]`` (row id or None) and catches every
  exception.

* **Own transaction.** The drop sites in the webhook handler may or may
  not commit downstream. We don't trust the caller's transaction — the
  observability row is written in a fresh ``SessionLocal()`` and
  committed on its own. That way the audit trail survives even if the
  outer transaction rolls back due to the very crash we're trying to
  log.

* **Privacy.** Phone numbers are masked at write time via the same
  helper the brain recorder uses. The raw E.164 number never enters
  ``ai_quality_events``.

* **No control-flow change.** Every wiring site is one extra call before
  ``return``. The dispatcher's existing logs stay; we only add a DB row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.ai_quality_events import mask_phone, _truncate

logger = logging.getLogger("nahla.inbound_observability")


# ── Categories the dashboard tabs filter on ─────────────────────────────────
#
# Adding a new bucket here requires:
#   1. Update the comment on ``AiQualityEvent.category`` in
#      ``database/models.py``.
#   2. Add a tab to ``dashboard/src/pages/AdminAiQuality.tsx``.
#   3. (Optional) Add a row to the per-category summary in
#      ``routers/admin_ai_quality.py``.
CATEGORY_AI_MISMATCH    = "ai_mismatch"     # brain post-compose (existing)
CATEGORY_INBOUND_DROP   = "inbound_drop"    # silent drop pre-brain
CATEGORY_WEBHOOK_ROUTING = "webhook_routing" # unrouted webhooks
CATEGORY_MEDIA_FAILURE  = "media_failure"   # reserved


VALID_CATEGORIES = frozenset({
    CATEGORY_AI_MISMATCH,
    CATEGORY_INBOUND_DROP,
    CATEGORY_WEBHOOK_ROUTING,
    CATEGORY_MEDIA_FAILURE,
})


# ── Mismatch type vocabulary for category != 'ai_mismatch' ──────────────────
#
# We deliberately keep ``mismatch_type`` a free string column (not an enum)
# so a new wiring site can ship without a migration. The vocabulary below
# is what the dashboard knows how to label in Arabic; unknown values are
# shown verbatim.

# inbound_drop sub-types
DROP_UNSUPPORTED_TYPE        = "unsupported_type"
DROP_EMPTY_TEXT              = "empty_text"
DROP_PRE_BRAIN_HANDOFF       = "pre_brain_handoff_drop"
DROP_DISPATCHER_EXCEPTION    = "dispatcher_exception"

# webhook_routing sub-types
ROUTE_UNROUTED_MISSING_PHONE = "unrouted_missing_phone_id"
ROUTE_UNROUTED_UNKNOWN_PHONE = "unrouted_unknown_phone_id"
ROUTE_UNROUTED_AMBIGUOUS     = "unrouted_ambiguous"
ROUTE_UNROUTED_WRONG_PROVIDER = "unrouted_wrong_provider"
ROUTE_UNROUTED_BAD_SECRET    = "unrouted_bad_secret"


# ── Core writer ─────────────────────────────────────────────────────────────


def _write_event(
    *,
    tenant_id: Optional[int],
    category: str,
    mismatch_type: str,
    customer_phone: str = "",
    conversation_id: Optional[int] = None,
    inbound_preview: str = "",
    detail: str = "",
    action_taken: str = "",
    chosen_path: str = "",
) -> Optional[int]:
    """Append one ``ai_quality_events`` row in a FRESH session.

    Returns the new row id on success, ``None`` on any failure.

    Why a fresh session: the call sites in the webhook handler may be
    about to roll back their own transaction (e.g. ``dispatcher_exception``
    fires from an outer ``except`` block where the parent SA session is
    already in an error state). Reusing that session would silently
    discard the observability row — exactly the kind of "the dashboard
    shows zeros while production drops messages" bug this module is
    supposed to PREVENT.

    Privacy: ``customer_phone`` is masked here; raw E.164 never
    enters the table.
    """
    # tenant_id is the only hard requirement — without it, the row can't
    # be scoped to a merchant and the dashboard would never surface it.
    # ``webhook_routing`` rows that pre-date tenant resolution use
    # ``tenant_id = 0`` (a sentinel the dashboard treats as "platform").
    try:
        tid = int(tenant_id) if tenant_id else 0
    except (TypeError, ValueError):
        tid = 0

    if category not in VALID_CATEGORIES:
        # Don't reject — log and write under a safe fallback so we
        # don't lose the signal because of a typo at a call site.
        logger.warning(
            "[INBOUND_OBS] unknown category=%r — coercing to inbound_drop",
            category,
        )
        category = CATEGORY_INBOUND_DROP

    try:
        # Lazy imports — models.py is heavy and we don't want it loaded
        # at module-import time on cold workers that may never hit a
        # drop site.
        from database.models import AiQualityEvent  # noqa: PLC0415
        from session import SessionLocal  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[INBOUND_OBS] import failed — event dropped: %s", exc)
        return None

    db = None
    try:
        db = SessionLocal()
        row = AiQualityEvent(
            tenant_id=tid,
            conversation_id=conversation_id if conversation_id else None,
            customer_phone_masked=mask_phone(customer_phone),
            category=category,
            mismatch_type=str(mismatch_type or "unknown")[:64],
            mismatch_reason=_truncate(detail, 500) if detail else None,
            action_taken=str(action_taken or "")[:64] or None,
            chosen_path=str(chosen_path or "")[:64] or None,
            # The brain context fields stay NULL for non-brain rows —
            # the dashboard's per-tab rendering hides them in the
            # inbound-drop / webhook-routing tabs anyway.
            inbound_preview=_truncate(inbound_preview) if inbound_preview else None,
            alignment_passed=False,
            regen_fired=False,
            resolved_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        new_id = int(getattr(row, "id", 0)) or None
        logger.info(
            "[INBOUND_OBS] recorded tenant=%s category=%s type=%s id=%s",
            tid, category, mismatch_type, new_id,
        )
        return new_id
    except Exception as exc:  # noqa: BLE001
        # Best-effort rollback + close. We must never raise.
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        logger.warning(
            "[INBOUND_OBS] persistence failed tenant=%s category=%s type=%s: %s",
            tid, category, mismatch_type, exc,
        )
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


# ── Public surface ──────────────────────────────────────────────────────────


def record_inbound_drop(
    *,
    tenant_id: Optional[int],
    drop_kind: str,
    customer_phone: str = "",
    conversation_id: Optional[int] = None,
    inbound_preview: str = "",
    detail: str = "",
    chosen_path: str = "",
) -> Optional[int]:
    """Record a pre-brain inbound drop.

    ``drop_kind`` is one of the ``DROP_*`` constants above (or a free
    string for a new site — the dashboard falls back to the literal).

    ``chosen_path`` lets the call site say where in the dispatcher the
    drop happened (e.g. ``"unsupported_type"``, ``"handoff_branch"``,
    ``"outer_except"``) — useful when one ``drop_kind`` can fire from
    multiple flows.

    Never raises.
    """
    return _write_event(
        tenant_id=tenant_id,
        category=CATEGORY_INBOUND_DROP,
        mismatch_type=drop_kind,
        customer_phone=customer_phone,
        conversation_id=conversation_id,
        inbound_preview=inbound_preview,
        detail=detail,
        chosen_path=chosen_path,
    )


def record_webhook_unrouted(
    *,
    tenant_id: Optional[int],
    sub_reason: str,
    phone_number_id: str = "",
    customer_phone: str = "",
    detail: str = "",
) -> Optional[int]:
    """Record a webhook that arrived but could not be routed to a tenant.

    ``sub_reason`` is one of the ``ROUTE_UNROUTED_*`` constants above:
    ``unrouted_missing_phone_id`` / ``unrouted_unknown_phone_id`` /
    ``unrouted_ambiguous`` / ``unrouted_wrong_provider`` /
    ``unrouted_bad_secret``.

    For ``unrouted_unknown_phone_id`` and ``unrouted_missing_phone_id``
    we typically don't have a ``tenant_id`` yet — the writer stores
    ``0`` (the platform sentinel) so the row remains queryable by an
    owner-level dashboard view.

    ``phone_number_id`` is stored in the ``detail`` payload so a
    follow-up can correlate with the merchant's ``WhatsAppConnection``.

    Never raises.
    """
    composed_detail = detail
    if phone_number_id:
        composed_detail = (
            f"phone_number_id={phone_number_id} | {detail}"
            if detail
            else f"phone_number_id={phone_number_id}"
        )
    return _write_event(
        tenant_id=tenant_id,
        category=CATEGORY_WEBHOOK_ROUTING,
        mismatch_type=sub_reason,
        customer_phone=customer_phone,
        detail=composed_detail,
    )


__all__ = [
    # Categories
    "CATEGORY_AI_MISMATCH",
    "CATEGORY_INBOUND_DROP",
    "CATEGORY_WEBHOOK_ROUTING",
    "CATEGORY_MEDIA_FAILURE",
    "VALID_CATEGORIES",
    # Drop-kind vocabulary
    "DROP_UNSUPPORTED_TYPE",
    "DROP_EMPTY_TEXT",
    "DROP_PRE_BRAIN_HANDOFF",
    "DROP_DISPATCHER_EXCEPTION",
    # Webhook routing vocabulary
    "ROUTE_UNROUTED_MISSING_PHONE",
    "ROUTE_UNROUTED_UNKNOWN_PHONE",
    "ROUTE_UNROUTED_AMBIGUOUS",
    "ROUTE_UNROUTED_WRONG_PROVIDER",
    "ROUTE_UNROUTED_BAD_SECRET",
    # Public writers
    "record_inbound_drop",
    "record_webhook_unrouted",
]
