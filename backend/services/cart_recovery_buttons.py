"""
services/cart_recovery_buttons.py
──────────────────────────────────
Dynamic-button factory for the abandoned-cart recovery workflow.

The merchant-facing spec asked for buttons that are TRULY dynamic — every
tap must be tied back to:

  • the customer who tapped it
  • the cart that was abandoned
  • (optionally) the coupon the cart was promised
  • the recovery stage the tap came from

So the button id is not just `resume_cart` — it's
`cart:resume_cart:cart_id=42:coupon=SAVE10:stage=2`. The webhook's button
handler parses that prefix and routes the tap to the right action while
recording the conversion against the parent AutomationExecution.

Two payload builders live here:

  • `build_template_components(...)`
        Used at stage 1, when we need to open a (potentially new)
        marketing conversation with a Meta-approved template. Produces the
        "components" array Meta expects for a template send, with the
        URL-button suffix carrying the cart_url so a tap goes straight to
        checkout — no extra hop.

  • `build_interactive_payload(...)`
        Used at stages 2+ when the customer-service window is still open.
        Produces a free-form interactive message with up to 3 reply
        buttons (Meta's hard limit) and supports a separate CTA-URL
        message for the coupon stage where the primary action is "open
        the cart with the discount applied".

The factory is intentionally small and pure — no DB, no I/O — so the
engine can call it inside the existing `_execute_action` path and the
test suite can pin the exact payload shape we ship to Meta.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlencode, urlparse, urlunparse

# ── Recovery-button action vocabulary ────────────────────────────────────────
#
# Every cart-recovery button id starts with this prefix so the webhook
# handler can dispatch a tap without needing to read the full action map.
# Adding a new action means: (a) add it here, (b) handle it in
# `routers/whatsapp_webhook._handle_cart_recovery_button`, (c) document
# it in the seed config so merchants can pick it from the dashboard.
ACTION_PREFIX = "cart"
ACTION_RESUME_CART   = "resume_cart"      # primary CTA → cart_url
ACTION_APPLY_COUPON  = "apply_coupon"     # primary CTA at stage 4
ACTION_ASK_QUESTION  = "ask_question"     # opens AI-assisted Q&A
ACTION_HUMAN_HELP    = "human_help"       # routes to the merchant inbox
ACTION_POSTPONE      = "postpone"         # silences future stages

KNOWN_ACTIONS = frozenset({
    ACTION_RESUME_CART,
    ACTION_APPLY_COUPON,
    ACTION_ASK_QUESTION,
    ACTION_HUMAN_HELP,
    ACTION_POSTPONE,
})


DeliveryMode = Literal["template", "interactive", "ai_recovery"]

# Delivery *policy* — what the merchant configures per step. The
# concrete wire format the engine ends up sending is one of
# :data:`DeliveryMode` and is resolved at send time by
# :func:`services.delivery_policy.resolve_delivery_mode` based on the
# live customer-service-window state and AI eligibility.
#
#   "auto"        — recommended default; pick the best legal mode
#   "template"    — always template (works inside or outside window)
#   "interactive" — interactive when window open, fallback otherwise
#   "ai_recovery" — Claude turn when window open + AI eligible, else fallback
PrimaryDeliveryMode = Literal["auto", "template", "interactive", "ai_recovery"]
FallbackDeliveryMode = Literal["template", "none"]


# ── id codec ────────────────────────────────────────────────────────────────

def encode_button_id(
    action: str,
    *,
    cart_id: Optional[Any] = None,
    coupon_code: Optional[str] = None,
    stage: Optional[int] = None,
    automation_id: Optional[int] = None,
) -> str:
    """
    Pack the action and its context into a Meta-safe button id.

    Meta caps button ids at 256 chars. Our scheme keeps each tap
    self-describing so a stale customer reply (button tapped a day later)
    still resolves to the right cart/coupon without needing to look up
    state from the customer's phone alone.
    """
    parts: List[str] = [ACTION_PREFIX, str(action)]
    if cart_id is not None:
        parts.append(f"c={cart_id}")
    if coupon_code:
        parts.append(f"k={coupon_code}")
    if stage is not None:
        parts.append(f"s={stage}")
    if automation_id is not None:
        parts.append(f"a={automation_id}")
    encoded = ":".join(parts)
    return encoded[:255]


def decode_button_id(button_id: str) -> Optional[Dict[str, Any]]:
    """
    Inverse of `encode_button_id`. Returns None when the id wasn't
    produced by this factory (so the webhook can fall through to its
    legacy handlers without raising).
    """
    if not button_id or not button_id.startswith(f"{ACTION_PREFIX}:"):
        return None
    chunks = button_id.split(":")
    if len(chunks) < 2:
        return None
    action = chunks[1]
    if action not in KNOWN_ACTIONS:
        return None
    out: Dict[str, Any] = {"action": action}
    for piece in chunks[2:]:
        if "=" not in piece:
            continue
        key, _, val = piece.partition("=")
        if key == "c":
            out["cart_id"] = val
        elif key == "k":
            out["coupon_code"] = val
        elif key == "s":
            try:
                out["stage"] = int(val)
            except ValueError:
                pass
        elif key == "a":
            try:
                out["automation_id"] = int(val)
            except ValueError:
                pass
    return out


# ── Default labels (Arabic, premium tone) ────────────────────────────────────
#
# Merchants can override every label from the dashboard via the per-step
# `cta_labels` config — these are the safe defaults that ship in the seed.
DEFAULT_LABELS_AR: Dict[str, str] = {
    ACTION_RESUME_CART:  "إكمال الطلب",
    ACTION_APPLY_COUPON: "استخدم الخصم الآن",
    ACTION_ASK_QUESTION: "عندي استفسار",
    ACTION_HUMAN_HELP:   "تحدث مع الدعم",
    ACTION_POSTPONE:     "لاحقاً",
}

DEFAULT_LABELS_EN: Dict[str, str] = {
    ACTION_RESUME_CART:  "Complete order",
    ACTION_APPLY_COUPON: "Use discount now",
    ACTION_ASK_QUESTION: "I have a question",
    ACTION_HUMAN_HELP:   "Talk to support",
    ACTION_POSTPONE:     "Later",
}


def label_for(action: str, language: str = "ar", overrides: Optional[Dict[str, str]] = None) -> str:
    """Return the merchant-edited label, or the default for the language."""
    if overrides and action in overrides and overrides[action]:
        return str(overrides[action])[:20]    # Meta cap: 20 chars per button
    table = DEFAULT_LABELS_EN if language == "en" else DEFAULT_LABELS_AR
    return table.get(action, action)[:20]


# ── Interactive (in-window) payload ──────────────────────────────────────────

def build_interactive_payload(
    *,
    to_phone: str,
    body_text: str,
    actions: List[str],
    language: str = "ar",
    cart_id: Optional[Any] = None,
    coupon_code: Optional[str] = None,
    stage: Optional[int] = None,
    automation_id: Optional[int] = None,
    cta_labels: Optional[Dict[str, str]] = None,
    footer_text: Optional[str] = "🐝 نحلة — مساعد متجرك",
    header_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the full Meta API body for an interactive message with up to 3
    dynamic reply buttons.

    The action list is a sequence of values from `KNOWN_ACTIONS`. Anything
    beyond the first three is silently dropped — Meta caps at 3 reply
    buttons per interactive message.
    """
    chosen = [a for a in actions if a in KNOWN_ACTIONS][:3]

    buttons = [
        {
            "type": "reply",
            "reply": {
                "id":    encode_button_id(
                    a,
                    cart_id=cart_id, coupon_code=coupon_code,
                    stage=stage, automation_id=automation_id,
                ),
                "title": label_for(a, language=language, overrides=cta_labels),
            },
        }
        for a in chosen
    ]

    interactive: Dict[str, Any] = {
        "type":   "button",
        "body":   {"text": body_text[:1024]},
        "action": {"buttons": buttons},
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text[:60]}
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}

    return {
        "messaging_product": "whatsapp",
        "to":                to_phone,
        "type":              "interactive",
        "interactive":       interactive,
    }


def build_cta_url_payload(
    *,
    to_phone: str,
    body_text: str,
    cta_label: str,
    cta_url: str,
    footer_text: Optional[str] = "🐝 نحلة — مساعد متجرك",
    header_text: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a Meta CTA-URL interactive message.

    Used for the coupon push at stage 4 where the *primary* visual is a
    big "Use the discount now" button that opens the cart_url with the
    coupon already attached. Meta only allows ONE CTA URL button per
    interactive message — that's the one bet we want at this stage.
    """
    interactive: Dict[str, Any] = {
        "type":   "cta_url",
        "body":   {"text": body_text[:1024]},
        "action": {
            "name":       "cta_url",
            "parameters": {
                "display_text": cta_label[:20],
                "url":          cta_url,
            },
        },
    }
    if header_text:
        interactive["header"] = {"type": "text", "text": header_text[:60]}
    if footer_text:
        interactive["footer"] = {"text": footer_text[:60]}
    return {
        "messaging_product": "whatsapp",
        "to":                to_phone,
        "type":              "interactive",
        "interactive":       interactive,
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

def attach_coupon_to_url(cart_url: str, coupon_code: Optional[str]) -> str:
    """
    Append `?coupon=CODE` (or `&coupon=CODE`) to the cart_url so a tap on
    the discount CTA lands in checkout with the code already applied.

    Storefronts that don't honour the coupon query param still receive a
    valid cart link — the worst case is the customer pasting the code
    manually, which is the same UX they'd get from a copy_code button.
    """
    if not cart_url:
        return cart_url
    if not coupon_code:
        return cart_url
    parsed = urlparse(cart_url)
    if parsed.query:
        new_query = parsed.query + "&" + urlencode({"coupon": coupon_code})
    else:
        new_query = urlencode({"coupon": coupon_code})
    return urlunparse(parsed._replace(query=new_query))


def stage_default_actions(stage: int, *, with_coupon: bool = False) -> List[str]:
    """
    Convenience: return the default action set for each recovery stage.

    Stage 1 (template)  → [resume_cart, ask_question, postpone]
    Stage 2 (interactive)→ [resume_cart, human_help, postpone]
    Stage 3 (ai_recovery)→ [] (handled outside the button factory)
    Stage 4 (coupon CTA) → [apply_coupon, resume_cart, ask_question]
    """
    if stage == 0:
        return [ACTION_RESUME_CART, ACTION_ASK_QUESTION, ACTION_POSTPONE]
    if stage == 1:
        return [ACTION_RESUME_CART, ACTION_HUMAN_HELP, ACTION_POSTPONE]
    if stage == 2:
        return []
    if with_coupon:
        return [ACTION_APPLY_COUPON, ACTION_RESUME_CART, ACTION_ASK_QUESTION]
    return [ACTION_RESUME_CART, ACTION_ASK_QUESTION, ACTION_POSTPONE]
