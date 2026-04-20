"""
services/cart_recovery_actions.py
─────────────────────────────────
Webhook-side handler for the dynamic abandoned-cart buttons.

The engine ships these buttons with `id`s produced by
`services.cart_recovery_buttons.encode_button_id`, so every tap arrives
back at the webhook fully self-describing — we know which cart, which
coupon (if any), which stage, and which automation triggered it without
hitting any per-tenant lookup tables.

This module owns three things:

  • Decoding the dynamic id and dispatching to the right action.
  • Stamping the conversion outcome onto the parent AutomationExecution
    + the parent AutomationEvent so the dashboard "stats_converted"
    counter and the recovery-funnel report stay accurate.
  • Producing the right WhatsApp follow-up: a deterministic CTA-URL for
    `resume_cart` / `apply_coupon`, an AI-Q&A handoff for
    `ask_question`, a human-handoff for `human_help`, and a quiet
    acknowledgement for `postpone` (which also silences the rest of
    the recovery thread).

Kept deliberately Rule-First — no Claude tokens are burned just for
identifying which button was tapped. The AI only enters the picture
when the customer actually picks `ask_question`, and even then we
hand off to the same Q&A path the inbound message handler uses.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy.orm.attributes import flag_modified

from services.cart_recovery_buttons import (
    ACTION_APPLY_COUPON,
    ACTION_ASK_QUESTION,
    ACTION_HUMAN_HELP,
    ACTION_POSTPONE,
    ACTION_PREFIX,
    ACTION_RESUME_CART,
    attach_coupon_to_url,
    decode_button_id,
)

logger = logging.getLogger("nahla.cart_recovery_actions")


# Type alias for the small set of webhook senders we need to call back
# into. We accept them as injected callables so this module stays free
# of any direct dependency on the FastAPI router (and stays test-friendly).
SendCtaUrl   = Callable[..., Awaitable[None]]
SendText     = Callable[..., Awaitable[None]]
SendButtons  = Callable[..., Awaitable[None]]


def is_cart_recovery_button(button_id: Optional[str]) -> bool:
    """Cheap prefix check for the webhook hot path."""
    return bool(button_id) and button_id.startswith(f"{ACTION_PREFIX}:")


async def handle_cart_recovery_button(
    *,
    db,
    button_id: str,
    phone_id: str,
    to_phone: str,
    tenant_id: Optional[int],
    send_cta_url: SendCtaUrl,
    send_text:    SendText,
    send_buttons: SendButtons,
) -> bool:
    """
    Dispatch a cart-recovery button tap. Returns True when the id was
    recognised and a reply was queued; False to let the webhook fall
    through to its legacy handlers.
    """
    decoded = decode_button_id(button_id)
    if not decoded:
        return False

    action: str = decoded.get("action", "")
    cart_id     = decoded.get("cart_id")
    coupon_code = decoded.get("coupon_code")
    stage       = decoded.get("stage")
    automation_id = decoded.get("automation_id")

    # Resolve the parent execution / event so we can stamp conversion +
    # find the original cart_url. Both lookups are best-effort — a
    # missing parent doesn't block the customer-facing reply.
    parent_event, parent_execution = _lookup_parent_records(
        db, tenant_id=tenant_id, cart_id=cart_id,
        automation_id=automation_id,
    )

    # Resolve the URL through a layered fallback so we never ship the
    # generic "أخبرني وش تحتاج" reply when a real checkout link exists.
    # Layer 1: the parent event payload (where the engine put it at
    # send-time). Layer 2: the durable Order row keyed by
    # ``cart-{cart_id}`` — survives even if the parent event was emitted
    # without a URL, which used to be the silent failure mode that made
    # "إكمال الطلب" look broken to merchants.
    cart_url = _resolve_cart_url(
        parent_event, db=db, tenant_id=tenant_id, cart_id=cart_id,
    )

    try:
        if action == ACTION_RESUME_CART:
            await _handle_resume(
                send_cta_url=send_cta_url, send_text=send_text,
                phone_id=phone_id, to_phone=to_phone,
                cart_url=cart_url, coupon_code=coupon_code,
                tenant_id=tenant_id, db=db,
            )
            _record_outcome(db, parent_event, parent_execution,
                            outcome="resume_cart", stage=stage)

        elif action == ACTION_APPLY_COUPON:
            await _handle_apply_coupon(
                send_cta_url=send_cta_url, send_text=send_text,
                phone_id=phone_id, to_phone=to_phone,
                cart_url=cart_url, coupon_code=coupon_code,
                tenant_id=tenant_id, db=db,
            )
            _record_outcome(db, parent_event, parent_execution,
                            outcome="apply_coupon", stage=stage,
                            extras={"coupon_code": coupon_code})

        elif action == ACTION_ASK_QUESTION:
            await _handle_ask_question(
                send_text=send_text, phone_id=phone_id, to_phone=to_phone,
                tenant_id=tenant_id, db=db,
            )
            _record_outcome(db, parent_event, parent_execution,
                            outcome="ask_question", stage=stage)

        elif action == ACTION_HUMAN_HELP:
            await _handle_human_help(
                db=db, send_text=send_text, phone_id=phone_id,
                to_phone=to_phone, tenant_id=tenant_id,
                parent_event=parent_event,
            )
            _record_outcome(db, parent_event, parent_execution,
                            outcome="human_help", stage=stage)

        elif action == ACTION_POSTPONE:
            await _handle_postpone(
                send_text=send_text, phone_id=phone_id, to_phone=to_phone,
                tenant_id=tenant_id, db=db,
                parent_event=parent_event,
                stage=stage,
            )
            _record_outcome(db, parent_event, parent_execution,
                            outcome="postpone", stage=stage,
                            extras={"rescheduled": True})

        else:
            logger.debug("[CartRecovery] Unhandled action=%s", action)
            return False

        # Persist execution + event payload mutations made by
        # _record_outcome / _handle_postpone.
        try:
            db.commit()
        except Exception:
            db.rollback()

        return True

    except Exception:
        logger.exception(
            "[CartRecovery] Action dispatch failed action=%s tenant=%s phone=%s",
            action, tenant_id, to_phone,
        )
        return False


# ── Action handlers ──────────────────────────────────────────────────────────

async def _handle_resume(
    *,
    send_cta_url: SendCtaUrl, send_text: SendText,
    phone_id: str, to_phone: str,
    cart_url: Optional[str], coupon_code: Optional[str],
    tenant_id: Optional[int], db,
) -> None:
    """
    Customer tapped "Complete order". Fire back a single CTA-URL message
    that lands them straight in checkout — coupon attached when one is
    in play. If we somehow lost the cart_url (very rare) we soft-fall to
    a friendly text rather than crashing.
    """
    final_url = attach_coupon_to_url(cart_url or "", coupon_code or None)
    if final_url:
        await send_cta_url(
            phone_id=phone_id, to=to_phone,
            body_text="ممتاز! تفضّل، السلة جاهزة للإكمال 🌟",
            btn_label="افتح السلة",
            btn_url=final_url,
            _tenant_id=tenant_id, _db=db,
        )
    else:
        # Authoritative miss: we couldn't resolve a checkout URL from
        # the event payload OR from the persisted Order row. This is a
        # rare edge (cart deleted between send and tap, or the merchant
        # disconnected the platform) — be HONEST instead of falling back
        # to a generic "tell me what you need" line that looks identical
        # to the merchant brain and made the button look broken.
        logger.warning(
            "[CartRecovery] resume_cart could not resolve checkout URL "
            "tenant=%s phone=%s — falling back to plain ack (no fake-AI prompt)",
            tenant_id, to_phone,
        )
        await send_text(
            phone_id=phone_id, to=to_phone,
            text=(
                "حاضر 🌷 سلتك لا تزال محفوظة لك في المتجر — افتحها وكمّل الطلب "
                "في أي وقت يناسبك."
            ),
            _tenant_id=tenant_id, _db=db,
        )


async def _handle_apply_coupon(
    *,
    send_cta_url: SendCtaUrl, send_text: SendText,
    phone_id: str, to_phone: str,
    cart_url: Optional[str], coupon_code: Optional[str],
    tenant_id: Optional[int], db,
) -> None:
    """
    Customer tapped the discount CTA. Always attach the code into the
    cart_url so storefronts that honour `?coupon=` apply it
    automatically; we still echo the code in the body so the customer
    can paste it manually on storefronts that don't.
    """
    final_url = attach_coupon_to_url(cart_url or "", coupon_code or None)
    body = (
        f"كود الخصم: *{coupon_code}*\nمطبّق تلقائياً في السلة 💎"
        if coupon_code else
        "تمام! خصمك جاهز — اضغط الزر تحت لإكمال الطلب 💎"
    )
    if final_url:
        await send_cta_url(
            phone_id=phone_id, to=to_phone,
            body_text=body,
            btn_label="استخدم الخصم الآن",
            btn_url=final_url,
            _tenant_id=tenant_id, _db=db,
        )
    else:
        await send_text(
            phone_id=phone_id, to=to_phone,
            text=body, _tenant_id=tenant_id, _db=db,
        )


async def _handle_ask_question(
    *,
    send_text: SendText, phone_id: str, to_phone: str,
    tenant_id: Optional[int], db,
) -> None:
    """
    Customer wants to ask something before completing. We don't burn
    AI tokens on a generated reply here — the next inbound text is
    routed to the standard AI Q&A path automatically. We just confirm
    we're listening so they feel acknowledged.
    """
    await send_text(
        phone_id=phone_id, to=to_phone,
        text=(
            "أكيد 🌷\n"
            "اكتب سؤالك هنا وأنا أساعدك فوراً — عن المنتج، التوصيل، "
            "الدفع، أو أي شي آخر."
        ),
        _tenant_id=tenant_id, _db=db,
    )


async def _handle_human_help(
    *,
    db, send_text: SendText,
    phone_id: str, to_phone: str,
    tenant_id: Optional[int],
    parent_event: Any,
) -> None:
    """
    Open a HandoffSession so the merchant inbox surfaces the cart and
    the AI stops auto-replying. Acknowledge to the customer so they
    don't keep waiting on the bot.
    """
    customer_name = "العميل"
    if parent_event is not None:
        try:
            from models import Customer
            cust = (
                db.query(Customer)
                .filter(
                    Customer.id == parent_event.customer_id,
                    Customer.tenant_id == tenant_id,
                )
                .first()
            )
            if cust and cust.name:
                customer_name = cust.name
        except Exception:
            pass

    if tenant_id is not None:
        try:
            from handoff.manager import create_handoff_session
            create_handoff_session(
                db=db, tenant_id=tenant_id,
                customer_phone=to_phone, customer_name=customer_name,
                last_message="[cart_recovery] requested human help",
                reason="cart_recovery_human_help",
                context_snapshot={
                    "cart_id":     (parent_event.payload or {}).get("cart_id")
                                    if parent_event else None,
                    "checkout_url": (parent_event.payload or {}).get("checkout_url")
                                    if parent_event else None,
                    "stage":       (parent_event.payload or {}).get("step_idx")
                                    if parent_event else None,
                },
            )
        except Exception:
            logger.exception(
                "[CartRecovery] Failed to open handoff tenant=%s phone=%s",
                tenant_id, to_phone,
            )

    await send_text(
        phone_id=phone_id, to=to_phone,
        text=(
            "تم تحويلك لفريق الدعم 👤\n"
            "راح يتواصل معك أحد ممثلينا في أقرب وقت 🌷"
        ),
        _tenant_id=tenant_id, _db=db,
    )


async def _handle_postpone(
    *,
    send_text: SendText, phone_id: str, to_phone: str,
    tenant_id: Optional[int], db,
    parent_event: Any,
    stage: Any = None,
) -> None:
    """
    Customer asked us to back off. The old behaviour was to silence
    every remaining stage — too aggressive: "later" does not mean
    "never". The new contract is a *reschedule*:

      1. Mark the NEXT pending stage as "rescheduled" in the parent
         event's `recovery_followups` so the sweeper treats it as
         already-emitted and doesn't fire it at its original time.
      2. Insert a fresh AutomationEvent for that same stage index with
         `created_at = now + postpone_reschedule_minutes` (default 12h,
         configurable per-automation).
      3. Send a short, calm acknowledgement.

    Stages AFTER the rescheduled one are left untouched — they keep
    their original timings. This keeps "postpone" as a local snooze
    rather than a workflow-ending veto.
    """
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    rescheduled_step_idx: Optional[int] = None
    reschedule_minutes = 720  # 12h default

    if parent_event is not None:
        payload = dict(parent_event.payload or {})
        payload["recovery_postponed_at"] = now_naive.isoformat()

        # Resolve the reschedule window from the automation's config.
        # The defaults already live in automations_seed.py; we just
        # honour overrides here.
        try:
            from models import SmartAutomation  # noqa: PLC0415
            auto_id = payload.get("automation_id")
            automation = None
            if auto_id:
                automation = (
                    db.query(SmartAutomation)
                    .filter(SmartAutomation.id == int(auto_id))
                    .first()
                )
            if automation is not None:
                cfg = dict(automation.config or {})
                configured = (
                    cfg.get("postpone_reschedule_minutes")
                    or cfg.get("postpone_delay_minutes")
                )
                if configured:
                    reschedule_minutes = max(30, int(configured))
        except Exception:
            pass

        # Figure out which stage to push out. We push the NEXT stage
        # after the tapped one — if the tap carried a stage index
        # we advance by 1; otherwise we find the smallest unfinished
        # step in recovery_followups and reschedule it.
        try:
            current_stage = int(stage) if stage is not None else 0
        except (TypeError, ValueError):
            current_stage = 0
        next_stage = current_stage + 1

        progress = list(payload.get("recovery_followups") or [])
        finished_steps = {int(p.get("step_idx", -1)) for p in progress}
        # If the "next" stage was already emitted, walk forward until
        # we find one that hasn't been yet. If every stage is done,
        # this is a no-op reschedule.
        max_cap = 1 + max([int(s) for s in finished_steps] + [current_stage, 0])
        while next_stage in finished_steps and next_stage < max_cap + 5:
            next_stage += 1

        progress.append({
            "step_idx":      next_stage,
            "skipped":       True,
            "reason":        "customer_postponed_rescheduled",
            "emitted_at":    now_naive.isoformat(),
            "rescheduled_for_minutes": reschedule_minutes,
        })
        payload["recovery_followups"] = progress
        parent_event.payload = payload
        try:
            flag_modified(parent_event, "payload")
        except Exception:
            pass
        rescheduled_step_idx = next_stage

        # Fire the re-queued event. We set a future `created_at` so the
        # engine's wait-loop quietly holds it until the clock catches
        # up — same pattern the conversion layer uses for the 10-min
        # active-conversation guard.
        try:
            from models import AutomationEvent  # noqa: PLC0415
            from datetime import timedelta  # noqa: PLC0415
            fire_at = now_naive + timedelta(minutes=reschedule_minutes)
            new_payload = dict(payload)
            new_payload["step_idx"] = int(next_stage)
            new_payload["parent_event_id"] = parent_event.id
            new_payload["reschedule_reason"] = "customer_postponed"
            # Only the fields the engine actually reads — don't carry
            # over recovery_followups (that lives on the parent).
            new_payload.pop("recovery_followups", None)
            new_payload.pop("recovery_taps", None)
            new_payload.pop("recovery_postponed_at", None)

            new_event = AutomationEvent(
                tenant_id   = parent_event.tenant_id,
                event_type  = parent_event.event_type or "cart_abandoned",
                customer_id = parent_event.customer_id,
                payload     = new_payload,
                processed   = False,
                created_at  = fire_at,
            )
            db.add(new_event)
        except Exception:
            logger.exception(
                "[CartRecovery] Failed to reschedule event=%s stage=%s",
                getattr(parent_event, "id", None), next_stage,
            )

    msg = (
        "تمام 👍\n"
        "سنذكّرك لاحقاً في وقت مناسب — السلة محفوظة لك."
    )
    await send_text(
        phone_id=phone_id, to=to_phone,
        text=msg,
        _tenant_id=tenant_id, _db=db,
    )

    logger.info(
        "[CartRecovery] postpone reschedule tenant=%s phone=%s next_stage=%s "
        "delay_minutes=%s",
        tenant_id, to_phone, rescheduled_step_idx, reschedule_minutes,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _lookup_parent_records(
    db, *, tenant_id: Optional[int], cart_id: Any, automation_id: Any,
):
    """
    Best-effort: locate the AutomationEvent + AutomationExecution that
    spawned the button. Returns (event, execution) — either may be None.
    """
    if tenant_id is None:
        return None, None

    try:
        from models import AutomationEvent, AutomationExecution
    except Exception:
        return None, None

    event = None
    if cart_id is not None:
        # We index events by the cart payload — most recent wins.
        event = (
            db.query(AutomationEvent)
            .filter(AutomationEvent.tenant_id == tenant_id)
            .filter(AutomationEvent.event_type == "cart_abandoned")
            .order_by(AutomationEvent.id.desc())
            .all()
        )
        # Cheap in-Python filter: avoids a JSON predicate that some
        # JSONB drivers fight with on the test SQLite path.
        match = None
        for ev in event:
            payload = ev.payload or {}
            cand_id = (
                payload.get("cart_id")
                or payload.get("cart_external_id")
                or payload.get("checkout_id")
            )
            if str(cand_id) == str(cart_id):
                match = ev
                break
        event = match

    execution = None
    if event is not None and automation_id:
        try:
            execution = (
                db.query(AutomationExecution)
                .filter(
                    AutomationExecution.tenant_id == tenant_id,
                    AutomationExecution.automation_id == int(automation_id),
                    AutomationExecution.event_id == event.id,
                )
                .order_by(AutomationExecution.id.desc())
                .first()
            )
        except Exception:
            execution = None

    return event, execution


def _resolve_cart_url(
    parent_event: Any,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    cart_id: Any = None,
) -> Optional[str]:
    """
    Three-tier resolution so a missing/empty payload never silently
    downgrades the customer to a generic chat fallback:

      1. ``parent_event.payload.{checkout_url, cart_url}`` —
         what the engine stamped at send-time. Authoritative when present.
      2. ``Order.checkout_url`` for the matching ``cart-{cart_id}`` row —
         the same column the dashboard reads from. Always populated by
         ``store_sync._normalise_abandoned_cart`` for Salla carts.
      3. ``None`` — caller decides what to do (the resume handler now
         shows a plain "open the store" message instead of a fake-AI
         line, so we never look like we forgot the cart).
    """
    if parent_event is not None:
        payload = parent_event.payload or {}
        from_event = payload.get("checkout_url") or payload.get("cart_url")
        if from_event:
            return from_event

    if db is not None and tenant_id is not None and cart_id:
        try:
            from models import Order  # noqa: PLC0415
            row = (
                db.query(Order)
                .filter(
                    Order.tenant_id == tenant_id,
                    Order.external_id == f"cart-{cart_id}",
                )
                .first()
            )
            if row and row.checkout_url:
                logger.info(
                    "[CartRecovery] resolved checkout_url from Order row "
                    "tenant=%s cart_id=%s (parent event payload was empty)",
                    tenant_id, cart_id,
                )
                return row.checkout_url
        except Exception:  # pragma: no cover - never let a fallback crash a tap
            logger.exception(
                "[CartRecovery] Order fallback lookup failed tenant=%s cart_id=%s",
                tenant_id, cart_id,
            )

    return None


def _record_outcome(
    db, parent_event: Any, parent_execution: Any,
    *, outcome: str, stage: Any, extras: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Stamp the conversion outcome onto the parent execution's
    `action_taken` and bump the parent automation's `stats_converted`
    when this is a positive signal.
    """
    extras = extras or {}
    if parent_execution is not None:
        action = dict(parent_execution.action_taken or {})
        responses = list(action.get("responses") or [])
        responses.append({
            "at":      datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "outcome": outcome,
            "stage":   stage,
            **extras,
        })
        action["responses"]      = responses
        action["last_outcome"]   = outcome
        action["last_outcome_at"] = responses[-1]["at"]

        # ── Conversion metrics — per-outcome counters stamped on the
        # same action_taken JSON so the dashboard funnel report can
        # read them back without joining against recovery_taps.
        metrics = dict(action.get("metrics") or {})
        metrics["clicked"]      = int(metrics.get("clicked", 0)) + 1
        if outcome == "resume_cart":
            metrics["resumed_cart"] = int(metrics.get("resumed_cart", 0)) + 1
            metrics["converted"]    = int(metrics.get("converted", 0)) + 1
        elif outcome == "apply_coupon":
            metrics["applied_coupon"] = int(metrics.get("applied_coupon", 0)) + 1
            metrics["converted"]      = int(metrics.get("converted", 0)) + 1
        elif outcome == "postpone":
            metrics["postponed"] = int(metrics.get("postponed", 0)) + 1
        action["metrics"] = metrics

        parent_execution.action_taken = action
        try:
            flag_modified(parent_execution, "action_taken")
        except Exception:
            pass

    if outcome in {"resume_cart", "apply_coupon"} and parent_execution is not None:
        try:
            from models import SmartAutomation
            auto = (
                db.query(SmartAutomation)
                .filter(SmartAutomation.id == parent_execution.automation_id)
                .first()
            )
            if auto is not None:
                auto.stats_converted = int(auto.stats_converted or 0) + 1
        except Exception:
            pass

    if parent_event is not None:
        payload = dict(parent_event.payload or {})
        taps = list(payload.get("recovery_taps") or [])
        taps.append({
            "at":      datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "outcome": outcome,
            "stage":   stage,
            **extras,
        })
        payload["recovery_taps"] = taps

        # ── Resume flattens the rest of the funnel ───────────────────
        # When the customer taps "resume_cart" (or "apply_coupon",
        # which is the same positive-intent signal delivered with a
        # discount), they've clearly decided to come back. Every
        # remaining automated nudge past this point would be noise,
        # so we pre-stamp the unfinished stages as "skipped" and the
        # emitter's sweeper will treat them as already-emitted.
        if outcome in {"resume_cart", "apply_coupon"}:
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            progress = list(payload.get("recovery_followups") or [])
            seen = {int(p.get("step_idx", -1)) for p in progress}
            try:
                current_stage = int(stage) if stage is not None else 0
            except (TypeError, ValueError):
                current_stage = 0
            for idx in range(current_stage + 1, 10):
                if idx in seen:
                    continue
                progress.append({
                    "step_idx":   idx,
                    "skipped":    True,
                    "reason":     f"user_{outcome}",
                    "emitted_at": now_iso,
                })
            payload["recovery_followups"] = progress
            payload["recovery_resumed_at"] = now_iso

        parent_event.payload = payload
        try:
            flag_modified(parent_event, "payload")
        except Exception:
            pass
