"""
services/conversion_layer.py
────────────────────────────
Conversion Layer for the abandoned-cart recovery workflow.

The lower layers (automation_emitters, automation_engine) answer the
question *WHEN* to reach out to a customer. This module answers the
harder question: *WHAT* to actually send, and whether sending is even
the right play right now.

It sits between `_active_step_for_event()` and the WhatsApp dispatch in
`_execute_action()`:

        emitter  →  engine  →  conversion_layer  →  dispatcher

The layer is deliberately Rule-First. No AI token is burned just to
decide whether to send the message — the AI only comes into play for
the optional stage-3 "AI recovery" turn, and even there only when
this layer has already concluded that the signals justify the spend.

────────────────────────────────────────────────────────────────────────
Single integration contract
────────────────────────────────────────────────────────────────────────
The engine calls `decide()` with the step the scheduler picked and a
ConversionContext it builds via `build_context()`. The function returns
a `ConversionDecision` that encodes four things:

    • proceed            — do we send at all?
    • skip_reason        — if not, why not (for the execution audit row)
    • reschedule_minutes — if > 0, the engine should re-queue this step
                            for that many minutes in the future
    • content overrides  — what buttons, what body, what coupon to use

The engine applies the overrides verbatim. It never second-guesses
them — that's what keeps the `WHAT` and the `WHEN` cleanly separated
and keeps this layer unit-testable without booting the whole stack.

────────────────────────────────────────────────────────────────────────
Dual-CTA rendering
────────────────────────────────────────────────────────────────────────
Meta's free-form interactive message supports EITHER a single CTA-URL
button OR up to three reply buttons — never both in the same payload.
The layer resolves that constraint by always rendering reply buttons:

    • no coupon   → single primary CTA: [resume_cart] (+ secondary esc)
    • coupon OK   → dual CTA:           [resume_cart] [apply_coupon]
                                         + one optional secondary

The coupon code itself is composed into the body in a copy-friendly
format (monospace, isolated line), so every WhatsApp client — Android,
iOS, desktop — can long-press-copy it. The `apply_coupon` button is
wired through `cart_recovery_actions._handle_apply_coupon`, which when
tapped responds with a CTA-URL message carrying the cart link with
`?coupon=CODE` already attached.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.conversion_layer")


# ── Tunable thresholds ───────────────────────────────────────────────────────
#
# These can be overridden per-automation via config, but the defaults
# below are the product-wide defaults that ship with every new tenant.
# All amounts are in the merchant's local currency (SAR for every KSA
# tenant at time of writing).
HIGH_VALUE_THRESHOLD_DEFAULT = 500.0    # triggers AI recovery on its own
MIN_COUPON_THRESHOLD_DEFAULT = 100.0    # below this, skip the coupon stage
ACTIVE_CONVERSATION_WINDOW_MINUTES = 10
POSTPONE_RESCHEDULE_MINUTES_DEFAULT = 720   # 12 h

COUPON_TIERS_DEFAULT: List[Dict[str, float]] = [
    # Ordered high→low — first match wins. The percentages are modest on
    # purpose: we'd rather leave 2-3% on the table than shred the
    # merchant's margin to force a sale.
    {"min_cart_value": 800.0, "percent": 5.0},
    {"min_cart_value": 300.0, "percent": 8.0},
    {"min_cart_value": 0.0,   "percent": 10.0},
]


# ── Context and decision dataclasses ─────────────────────────────────────────

@dataclass
class ConversionContext:
    """
    Everything the conversion layer needs to decide what (if anything)
    to send right now. Built once per engine tick from the live event,
    customer, config, and recent message history.

    Kept intentionally flat — no SQLAlchemy objects, no nested models —
    so unit tests can instantiate it directly without touching a DB.
    """
    # ── Identity
    customer_id:        Optional[int] = None
    customer_phone:     str = ""
    tenant_id:          Optional[int] = None
    automation_id:      Optional[int] = None
    event_id:           Optional[int] = None
    stage:              int = 0

    # ── Cart state
    cart_id:            Optional[str] = None
    cart_value:         float = 0.0
    cart_items:         int = 0
    cart_age:           timedelta = timedelta(0)
    cart_url:           Optional[str] = None
    store_url:          Optional[str] = None

    # ── Customer history
    previous_orders:    int = 0
    messages_count:     int = 0           # inbound messages in last 24h
    buttons_clicked:    List[str] = field(default_factory=list)
    last_action:        Optional[str] = None
    last_inbound_at:    Optional[datetime] = None

    # ── Guardrails
    order_completed:    bool = False
    customer_blocked:   bool = False
    customer_opted_out: bool = False

    # ── Inference helpers
    @property
    def customer_interacted(self) -> bool:
        """Any signal that the customer is still paying attention."""
        return bool(self.buttons_clicked) or self.messages_count > 0

    @property
    def has_cart_link(self) -> bool:
        return bool(self.cart_url)

    @property
    def has_any_destination(self) -> bool:
        return bool(self.cart_url or self.store_url)


@dataclass
class ConversionDecision:
    """
    The layer's answer to "should we send, and if so, what?". The
    engine treats this as a read-only contract.
    """
    proceed: bool = True
    skip_reason: Optional[str] = None
    reschedule_minutes: int = 0

    # Content overrides. None = "use whatever the step config already says".
    delivery_mode_override: Optional[str] = None
    buttons_override: Optional[List[str]] = None
    body_text_override: Optional[str] = None
    cta_labels_override: Optional[Dict[str, str]] = None

    # Coupon plan (only consumed on the coupon stage).
    coupon_granted: bool = False
    coupon_percent: Optional[float] = None
    coupon_code: Optional[str] = None    # None = let the engine resolve it

    # Free-form audit blob — stamped onto AutomationExecution.action_taken
    # so the dashboard can explain every decision.
    audit: Dict[str, Any] = field(default_factory=dict)


# ── Rule primitives (pure, unit-testable) ────────────────────────────────────

def should_trigger_ai_recovery(
    ctx: ConversionContext, *,
    high_value_threshold: float = HIGH_VALUE_THRESHOLD_DEFAULT,
) -> bool:
    """
    Only burn AI tokens when there is a real signal of intent.

    Triggers on ANY of:
      • the customer interacted (button tap or inbound message)
      • the cart value is above the high-value threshold
      • the last button tap was one of the "still thinking" signals
    """
    if ctx.customer_interacted:
        return True
    if ctx.cart_value > high_value_threshold:
        return True
    if ctx.last_action in ("ask_question", "postpone", "still_thinking"):
        return True
    return False


def should_send_coupon(
    ctx: ConversionContext, *,
    min_cart_value: float = MIN_COUPON_THRESHOLD_DEFAULT,
) -> bool:
    """
    Three hard nos, then default yes:

      • the customer already converted — stop selling
      • the last visible action was resume_cart — they're on the path,
        don't undercut your own margin
      • the cart value is below the minimum — the coupon wouldn't
        move the needle and would just train the customer to wait
        for discounts
    """
    if ctx.order_completed:
        return False
    if ctx.last_action == "resume_cart":
        return False
    if ctx.cart_value < min_cart_value:
        return False
    return True


def dynamic_coupon_engine(
    ctx: ConversionContext, *,
    tiers: Optional[List[Dict[str, float]]] = None,
) -> float:
    """
    Pick the coupon percentage for this cart. Sliding scale protects
    merchant margin on big carts (where a flat 10% is unaffordable)
    while giving smaller carts the nudge they actually need.
    """
    schedule = tiers or COUPON_TIERS_DEFAULT
    for tier in schedule:
        if ctx.cart_value >= float(tier.get("min_cart_value", 0)):
            return float(tier.get("percent", 10.0))
    return 10.0


def is_active_conversation(ctx: ConversionContext, *, window_minutes: int = ACTIVE_CONVERSATION_WINDOW_MINUTES) -> bool:
    """
    True when the customer has sent us a message in the last N minutes.
    Used to avoid interrupting a live exchange with a scheduled nudge.
    """
    if ctx.last_inbound_at is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = ctx.last_inbound_at
    if last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - last) < timedelta(minutes=window_minutes)


def is_killed(ctx: ConversionContext) -> bool:
    """Hard kill — never send, never reschedule."""
    return bool(ctx.customer_blocked or ctx.customer_opted_out)


# ── Button intelligence ──────────────────────────────────────────────────────

_BUTTONS_BY_STAGE: Dict[int, List[str]] = {
    # Stage numbers are 0-indexed to match config.steps[].
    0: ["resume_cart", "ask_question", "postpone"],
    1: ["resume_cart", "human_help",   "postpone"],
    2: ["resume_cart", "ask_question", "postpone"],     # AI recovery
    3: ["resume_cart", "ask_question", "human_help"],   # coupon step — BASE set (no coupon granted)
}
_BUTTONS_COUPON_STAGE_WITH_COUPON: List[str] = [
    "resume_cart", "apply_coupon", "ask_question",
]


def buttons_for_stage(stage: int, *, coupon_granted: bool) -> List[str]:
    """
    Per-stage default button set — can be overridden by the merchant's
    per-step `buttons` config.

    The coupon stage is the only one whose buttons depend on runtime
    state: if the conversion layer decides against a coupon, we drop
    `apply_coupon` from the list so the customer never sees a coupon
    CTA they can't act on.
    """
    if stage == 3 and coupon_granted:
        return list(_BUTTONS_COUPON_STAGE_WITH_COUPON)
    return list(_BUTTONS_BY_STAGE.get(stage, ["resume_cart", "ask_question", "postpone"]))


# ── Premium coupon presentation ──────────────────────────────────────────────

def format_coupon_block(
    code: str,
    percent: Optional[float] = None,
    *,
    language: str = "ar",
) -> str:
    """
    Render the coupon as a copy-friendly block. Used both as a complete
    body (when the merchant hasn't authored a template) and as a
    drop-in replacement for `{{discount_code}}` inside a merchant body.

    Example output (Arabic):

        🎁 كود خصم خاص لك
        ━━━━━━━━━━━━━━━━
        `SAVE10`   (٪10)
        ━━━━━━━━━━━━━━━━
        اضغط مطوّلاً على الكود لنسخه، أو استخدم الزر أدناه.
    """
    code = (code or "").strip()
    if not code:
        return ""
    if language == "en":
        header  = "🎁 A discount — just for you"
        ruler   = "━━━━━━━━━━━━━━━━"
        pct_sfx = f"  ({int(percent)}% off)" if percent else ""
        hint    = "Long-press the code to copy it, or tap the button below."
    else:
        header  = "🎁 كود خصم خاص لك"
        ruler   = "━━━━━━━━━━━━━━━━"
        pct_sfx = f"  (خصم ٪{int(percent)})" if percent else ""
        hint    = "اضغط مطوّلاً على الكود لنسخه، أو استخدم الزر أدناه."
    return (
        f"{header}\n"
        f"{ruler}\n"
        f"`{code}`{pct_sfx}\n"
        f"{ruler}\n"
        f"{hint}"
    )


def enrich_body_with_coupon(body: str, code: str, percent: Optional[float], *, language: str = "ar") -> str:
    """
    If the merchant's body template contains `{{discount_code}}`, we
    splice the formatted coupon block in place of the raw placeholder.
    Otherwise we append the block to the body so the code is always
    present even on ad-hoc copy.
    """
    block = format_coupon_block(code, percent, language=language)
    if not block:
        return body
    if "{{discount_code}}" in (body or ""):
        return body.replace("{{discount_code}}", f"`{code}`")
    # No placeholder — just append so merchants who forgot still ship a
    # valid message.
    return f"{(body or '').rstrip()}\n\n{block}" if body else block


# ── Context builder (reads DB, best-effort) ──────────────────────────────────

def build_context(
    db: Any, *,
    tenant_id: int,
    event: Any,
    customer: Any,
    automation: Any,
    active_step: Dict[str, Any],
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> ConversionContext:
    """
    Assemble a ConversionContext from the live DB state.

    Every DB lookup here is best-effort: failures are swallowed and
    default values are kept. The engine must never fail a send just
    because we couldn't count previous orders.
    """
    now_naive = (now or datetime.now(timezone.utc).replace(tzinfo=None))
    if now_naive.tzinfo is not None:
        now_naive = now_naive.astimezone(timezone.utc).replace(tzinfo=None)

    payload: Dict[str, Any] = dict(getattr(event, "payload", None) or {})

    # ── Identity / stage
    stage = 0
    try:
        stage = int(payload.get("step_idx") or 0)
    except (TypeError, ValueError):
        stage = 0

    # ── Cart
    cart_url = (
        payload.get("checkout_url")
        or payload.get("cart_url")
        or None
    )
    store_url = (
        payload.get("store_url")
        or config.get("store_url")
        or None
    )
    cart_value = _safe_float(
        payload.get("cart_total")
        or payload.get("total")
        or payload.get("order_total")
    )
    cart_items = _safe_int(
        payload.get("items")
        or payload.get("item_count")
        or payload.get("line_items_count")
    )
    cart_id = (
        payload.get("cart_id")
        or payload.get("cart_external_id")
        or payload.get("checkout_id")
    )
    created_at = getattr(event, "created_at", None)
    if isinstance(created_at, datetime):
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        cart_age = now_naive - created_at
    else:
        cart_age = timedelta(0)

    # ── Find the parent (stage-1) event's recovery_taps log so we know
    # which buttons this customer has tapped across the whole thread,
    # not just on the current stage.
    parent_payload = payload
    parent_event_id = payload.get("parent_event_id") or getattr(event, "id", None)
    if stage > 0 and parent_event_id:
        try:
            from models import AutomationEvent  # noqa: PLC0415
            parent = (
                db.query(AutomationEvent)
                .filter(
                    AutomationEvent.tenant_id == tenant_id,
                    AutomationEvent.id == parent_event_id,
                )
                .first()
            )
            if parent is not None and parent.payload:
                parent_payload = dict(parent.payload)
        except Exception:
            pass

    taps = parent_payload.get("recovery_taps") or []
    buttons_clicked: List[str] = [
        str(t.get("outcome")) for t in taps if t.get("outcome")
    ]
    last_action = buttons_clicked[-1] if buttons_clicked else None

    # ── Customer signals
    customer_id    = getattr(customer, "id", None)
    customer_phone = getattr(customer, "phone", "") or ""
    customer_meta  = _customer_metadata(customer)
    customer_blocked   = bool(
        customer_meta.get("blocked") or customer_meta.get("is_blocked")
    )
    customer_opted_out = bool(
        customer_meta.get("marketing_opt_out")
        or customer_meta.get("opted_out")
        or customer_meta.get("do_not_contact")
    )

    previous_orders = _count_previous_orders(db, tenant_id=tenant_id, customer_id=customer_id)

    # ── Conversion guard: any real order since the cart was abandoned?
    order_completed = False
    try:
        from core.automation_emitters import (  # noqa: PLC0415
            _customer_has_completed_order_since, _naive,
        )
        if customer_id:
            since = _naive(created_at) if isinstance(created_at, datetime) else now_naive
            order_completed = _customer_has_completed_order_since(
                db, tenant_id=tenant_id, customer_id=customer_id, since=since,
            )
    except Exception:
        pass

    # ── Recent inbound message count + last_inbound_at
    messages_count, last_inbound_at = _recent_inbound_stats(
        db, tenant_id=tenant_id, phone=customer_phone, now=now_naive,
    )

    return ConversionContext(
        customer_id       = customer_id,
        customer_phone    = customer_phone,
        tenant_id         = tenant_id,
        automation_id     = getattr(automation, "id", None),
        event_id          = getattr(event, "id", None),
        stage             = stage,

        cart_id           = str(cart_id) if cart_id is not None else None,
        cart_value        = cart_value,
        cart_items        = cart_items,
        cart_age          = cart_age,
        cart_url          = cart_url,
        store_url         = store_url,

        previous_orders   = previous_orders,
        messages_count    = messages_count,
        buttons_clicked   = buttons_clicked,
        last_action       = last_action,
        last_inbound_at   = last_inbound_at,

        order_completed   = order_completed,
        customer_blocked  = customer_blocked,
        customer_opted_out= customer_opted_out,
    )


# ── The decision ─────────────────────────────────────────────────────────────

def decide(
    ctx: ConversionContext,
    *,
    active_step: Dict[str, Any],
    config: Dict[str, Any],
) -> ConversionDecision:
    """
    Given the context and the step the scheduler picked, decide how
    (and whether) to send.

    Guardrails in priority order:

      1. Kill switch       — blocked / opted_out  ⇒ never send, no reschedule
      2. Already converted — customer placed an order since abandon
      3. Active conversation — customer is actively chatting, don't
         interrupt — just reschedule to right after the window closes.
      4. AI recovery gate  — only for stage 3 and only when signals allow
      5. Coupon gate       — only for coupon stage and only when the
                              customer's context makes a coupon worthwhile

    Side output: the `audit` blob carries every input that fed into the
    decision so the execution row can explain itself in the dashboard.
    """
    decision = ConversionDecision(audit={
        "stage":            ctx.stage,
        "cart_value":       ctx.cart_value,
        "cart_items":       ctx.cart_items,
        "cart_age_minutes": int(ctx.cart_age.total_seconds() // 60),
        "previous_orders":  ctx.previous_orders,
        "buttons_clicked":  list(ctx.buttons_clicked),
        "last_action":      ctx.last_action,
        "messages_count":   ctx.messages_count,
    })

    # ── 1. Kill switch
    if is_killed(ctx):
        decision.proceed = False
        decision.skip_reason = (
            "customer_blocked" if ctx.customer_blocked
            else "customer_opted_out"
        )
        return decision

    # ── 2. Already converted
    if ctx.order_completed:
        decision.proceed = False
        decision.skip_reason = "order_completed"
        return decision

    # ── 3. Active conversation — reschedule past the window instead of
    # killing the step outright.
    if is_active_conversation(ctx):
        decision.proceed = False
        decision.skip_reason = "user_active"
        decision.reschedule_minutes = ACTIVE_CONVERSATION_WINDOW_MINUTES + 5
        return decision

    message_type = str(active_step.get("message_type") or "")
    delivery_mode = str(active_step.get("delivery_mode") or "template")
    high_value_threshold = float(
        active_step.get("high_value_threshold")
        or config.get("high_value_threshold")
        or HIGH_VALUE_THRESHOLD_DEFAULT
    )
    min_coupon_threshold = float(
        active_step.get("min_coupon_threshold")
        or config.get("min_coupon_threshold")
        or MIN_COUPON_THRESHOLD_DEFAULT
    )

    # ── 4. AI recovery gate — stage-3 only
    if delivery_mode == "ai_recovery" or message_type == "ai_recovery":
        if not should_trigger_ai_recovery(ctx, high_value_threshold=high_value_threshold):
            decision.proceed = False
            decision.skip_reason = "no_signal"
            decision.audit["ai_gate"] = "no_signal"
            return decision
        decision.audit["ai_gate"] = "fire"

    # ── 5. Coupon gate — stage-4 only
    coupon_granted = False
    coupon_percent: Optional[float] = None
    if delivery_mode == "interactive" and (
        message_type == "coupon" or active_step.get("auto_coupon") is True
    ):
        if should_send_coupon(ctx, min_cart_value=min_coupon_threshold):
            coupon_granted = True
            tiers = (
                active_step.get("coupon_tiers")
                or config.get("coupon_tiers")
                or COUPON_TIERS_DEFAULT
            )
            coupon_percent = dynamic_coupon_engine(ctx, tiers=tiers)
            decision.audit["coupon_gate"]    = "grant"
            decision.audit["coupon_percent"] = coupon_percent
        else:
            decision.audit["coupon_gate"] = "skip"

    decision.coupon_granted = coupon_granted
    decision.coupon_percent = coupon_percent

    # ── Button intelligence — unless the merchant pinned buttons in
    # config, pick them by stage and (for stage 4) by coupon state.
    if not active_step.get("buttons"):
        decision.buttons_override = buttons_for_stage(
            ctx.stage, coupon_granted=coupon_granted,
        )
    else:
        # If the merchant pinned apply_coupon on a no-coupon render,
        # strip it so we don't show a dead button.
        pinned = list(active_step["buttons"])
        if "apply_coupon" in pinned and not coupon_granted:
            decision.buttons_override = [b for b in pinned if b != "apply_coupon"]

    # Fallback action when cart_url is missing but store_url exists:
    # rewrite resume_cart → open_store so the dynamic button still
    # lands somewhere useful.
    if not ctx.cart_url and ctx.store_url:
        bl = decision.buttons_override or list(active_step.get("buttons") or [])
        if "resume_cart" in bl:
            bl = [("open_store" if b == "resume_cart" else b) for b in bl]
            decision.buttons_override = bl
            decision.audit["cta_fallback"] = "open_store"

    return decision


# ── Private helpers ──────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v: Any) -> int:
    try:
        if v is None or v == "":
            return 0
        if isinstance(v, list):
            return len(v)
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _customer_metadata(customer: Any) -> Dict[str, Any]:
    if customer is None:
        return {}
    meta = getattr(customer, "extra_metadata", None)
    if isinstance(meta, dict):
        return meta
    return {}


def _count_previous_orders(db: Any, *, tenant_id: int, customer_id: Optional[int]) -> int:
    if not customer_id:
        return 0
    try:
        from models import Order  # noqa: PLC0415
        from core.automation_emitters import _resolve_order_customer  # noqa: PLC0415
    except Exception:
        return 0
    try:
        orders = (
            db.query(Order)
            .filter(Order.tenant_id == tenant_id)
            .all()
        )
    except Exception:
        return 0
    n = 0
    for o in orders:
        if getattr(o, "status", None) in {"cancelled", "refunded", "pending_confirmation"}:
            continue
        try:
            candidate = _resolve_order_customer(db, tenant_id, o)
        except Exception:
            candidate = None
        if candidate is not None and getattr(candidate, "id", None) == customer_id:
            n += 1
    return n


def _recent_inbound_stats(
    db: Any, *, tenant_id: int, phone: str, now: datetime,
) -> tuple[int, Optional[datetime]]:
    """
    (count_last_24h, last_inbound_at) — uses the MessageEvent table the
    conversation engine writes into. Both values are best-effort and
    fall back to (0, None) on any error.
    """
    if not phone:
        return 0, None
    try:
        from models import MessageEvent  # noqa: PLC0415
    except Exception:
        return 0, None
    try:
        horizon = now - timedelta(hours=24)
        rows = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.direction == "inbound",
                MessageEvent.created_at >= horizon,
            )
            .all()
        )
    except Exception:
        return 0, None

    count = 0
    last: Optional[datetime] = None
    for r in rows:
        meta = getattr(r, "extra_metadata", None) or {}
        if not isinstance(meta, dict):
            continue
        if str(meta.get("phone") or "") != phone:
            continue
        count += 1
        created = getattr(r, "created_at", None)
        if isinstance(created, datetime):
            if created.tzinfo is not None:
                created = created.astimezone(timezone.utc).replace(tzinfo=None)
            if last is None or created > last:
                last = created
    return count, last
