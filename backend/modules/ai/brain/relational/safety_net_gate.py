"""
brain/relational/safety_net_gate.py
───────────────────────────────────
Commit 3 of the Tenant 33 #49 Conversational Commerce Architecture
rollout: a SURGICAL safety-net suppression gate.

Strict invariants pinned by the merchant directive
──────────────────────────────────────────────────
  1. NO global suppression. Every (net_name, moment) entry is
     listed explicitly in :data:`_SUPPRESSION_TABLE`. Adding a
     row requires a deliberate commit + a regression test.

  2. NO cascade. The gate is consulted INDEPENDENTLY for each
     suppressible net. There is no "skip everything for moment X"
     mode.

  3. The whitelist of suppressible nets is CLOSED to two cold-info
     nets only:
       * ``store_link`` — :func:`apply_store_link_safety_net`
       * ``location``   — :func:`apply_location_safety_net`

  4. The blocklist of NEVER-suppressible nets is enforced by an
     architectural test:
       * ``product``                  (product-card delivery)
       * ``media_key``                (media artifact delivery)
       * ``staff_contact``            (vCard / staff phone delivery)
       * ``delivery_info_context``    (order-completion fix #45)
       * ``product_reask_guard``      (order-completion fix #47)
       * ``outbound_artifact_guard``  (final hollow-affirmation guard)
       * ``clear_intent_fallback``    (timeout-recovery rewrite)
       * Any net whose name contains ``payment`` / ``receipt`` /
         ``order`` / ``handoff`` / ``takeover`` / ``media`` /
         ``artifact``.

  5. NO state mutation. NO reply-text rewriting. NO selection of
     wording. The gate only decides whether a SPECIFIC named net
     gets a chance to fire.

  6. The relational layer "may shape the conversation, but must
     never fabricate business state" — the same rule from
     :mod:`relational.contracts` applies here.

Behaviour matrix
────────────────
  flag off                     -> never suppresses
  ctx.relational_state is None -> never suppresses
  moment == NONE               -> never suppresses
  net_name not whitelisted     -> never suppresses
  (net, moment) not in table   -> never suppresses
  rule matches                 -> returns ``(True, reason_token)``
                                  caller skips the net AND emits the
                                  ``[CX] safety_net_suppressed`` log line.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, FrozenSet, Optional, Tuple, Union

from .moments import ConversationMoment
from .state import RelationalState

logger = logging.getLogger("nahla.relational.safety_net_gate")


# ── 1. Whitelist of nets the gate is ALLOWED to suppress ─────────────
# Adding a net here requires the merchant directive level (it changes
# what the bot is allowed to NOT say). Do not extend lightly.
SUPPRESSIBLE_NETS: FrozenSet[str] = frozenset({
    "store_link",   # apply_store_link_safety_net  (cold URL injection)
    "location",     # apply_location_safety_net    (cold maps URL injection)
})


# ── 2. Hard-locked blocklist (architectural invariant). NEVER append. ─
# Tested in ``test_relational_safety_net_suppression.py`` against the
# suppression table — the build fails if any of these names appear as
# the first element of a suppression-table key.
NEVER_SUPPRESSIBLE_NETS: FrozenSet[str] = frozenset({
    # Order-critical
    "product",
    "delivery_info_context",
    "product_reask_guard",
    # Media / outbound artifact pipeline
    "media_key",
    "outbound_artifact_guard",
    # Handoff / staff routing
    "staff_contact",
    # Recovery rewrites
    "clear_intent_fallback",
    # Payment-side and order-side never even appear as nets here, but
    # we list canonical tokens so the architectural test can grep
    # the table for any business-state name leak.
    "payment",
    "payment_receipt",
    "payment_link",
    "receipt",
    "order",
    "order_status",
    "order_paid",
    "tracking",
    "shipment",
    "handoff",
    "takeover",
    "manual_takeover",
})


# ── 3. The suppression table (CLOSED). Edit reviewed = merchant level.
#
# Keys:    (net_name, moment)
# Values:  short reason token used in the log line.
_SUPPRESSION_TABLE: Dict[Tuple[str, ConversationMoment], str] = {
    # PRAISE_POST_DELIVERY:
    #   The brain composed a warm reply to a post-delivery praise.
    #   A cold "موقعنا 📍" / "تفضل رابط متجرنا" injection here is
    #   the exact bug the May 2026 audit closed. Suppress.
    ("store_link", ConversationMoment.PRAISE_POST_DELIVERY): "praise_warmth_priority",
    ("location",   ConversationMoment.PRAISE_POST_DELIVERY): "praise_warmth_priority",

    # COMPLAINT_*: the brain produced a complaint-recovery reply
    # (Commit 2 routed HANDOFF → LLM_REPLY with response_goal=
    # complaint_recovery_*). A store-link / maps-URL injection on
    # top of the recovery reply is informational noise that derails
    # the empathy. Suppress.
    ("store_link", ConversationMoment.COMPLAINT_SHIPPING_DELAY):  "complaint_recovery_priority",
    ("location",   ConversationMoment.COMPLAINT_SHIPPING_DELAY):  "complaint_recovery_priority",
    ("store_link", ConversationMoment.COMPLAINT_PRODUCT_QUALITY): "complaint_recovery_priority",
    ("location",   ConversationMoment.COMPLAINT_PRODUCT_QUALITY): "complaint_recovery_priority",
    ("store_link", ConversationMoment.COMPLAINT_GENERIC):         "complaint_recovery_priority",
    ("location",   ConversationMoment.COMPLAINT_GENERIC):         "complaint_recovery_priority",
}


# ── 4. Kill switch (independent flag, default OFF) ──────────────────


def is_safety_net_suppression_enabled() -> bool:
    """``True`` when ``RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED`` is
    set to a truthy value.

    Independent from ``RELATIONAL_LAYER_ENABLED`` (Commit 1) and
    ``RELATIONAL_DECISION_ROUTER_ENABLED`` (Commit 2). All three
    default OFF — staged rollout per merchant directive.
    """
    raw = (os.environ.get("RELATIONAL_SAFETY_NET_SUPPRESSION_ENABLED") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── 5. Core gate function ───────────────────────────────────────────


def should_suppress_safety_net(
    *,
    net_name: str,
    moment: Union[ConversationMoment, str, None],
    relational_state: Optional[RelationalState] = None,
) -> Tuple[bool, str]:
    """Return ``(suppress, reason_token)`` for a single (net, moment)
    decision. NEVER raises.

    Resolution order:
      1. Kill switch off              -> (False, "flag_off")
      2. ``moment`` is None / NONE     -> (False, "no_moment")
      3. ``net_name`` not whitelisted  -> (False, "net_not_suppressible")
      4. ``net_name`` blocklisted      -> (False, "net_protected")
         (defensive — would also fail step 3, but explicit beats implicit.)
      5. (net, moment) NOT in table    -> (False, "no_rule")
      6. rule matches                  -> (True, reason_from_table)

    Parameters
    ----------
    net_name:
        Short token, e.g. ``"location"`` / ``"store_link"``. Match
        is case-sensitive — callers must pass the canonical token.
    moment:
        Either a ``ConversationMoment`` value, the string token
        (``"praise_post_delivery"``), or ``None``. ``None`` /
        ``ConversationMoment.NONE`` always returns inert.
    relational_state:
        Optional — when present, used for documentation / logging
        only. The decision is based on ``moment`` alone so callers
        can pass a moment without holding the full state.

    Returns
    -------
    Tuple[bool, str]
        ``(True, reason)`` to suppress the net; ``(False, why)``
        otherwise. ``why`` is a short stable token suitable for
        the structured log line.
    """
    try:
        if not is_safety_net_suppression_enabled():
            return False, "flag_off"

        moment_enum = _coerce_moment(moment)
        if moment_enum is None or moment_enum == ConversationMoment.NONE:
            return False, "no_moment"

        net_key = str(net_name or "").strip().lower()
        if not net_key:
            return False, "no_net_name"

        # Architectural defence-in-depth. The whitelist is
        # authoritative; the blocklist is an explicit secondary
        # guard so a future drift can't silently extend the table.
        if net_key in NEVER_SUPPRESSIBLE_NETS:
            return False, "net_protected"
        if net_key not in SUPPRESSIBLE_NETS:
            return False, "net_not_suppressible"

        reason = _SUPPRESSION_TABLE.get((net_key, moment_enum))
        if not reason:
            return False, "no_rule"

        return True, reason
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CX] safety_net_gate failed (returning inert): %s", exc,
        )
        return False, "exception"


def _coerce_moment(
    moment: Union[ConversationMoment, str, None],
) -> Optional[ConversationMoment]:
    if moment is None:
        return None
    if isinstance(moment, ConversationMoment):
        return moment
    if isinstance(moment, str):
        try:
            return ConversationMoment(moment.strip().lower())
        except Exception:
            return None
    return None


# ── 6. Log emission helper ──────────────────────────────────────────


def log_safety_net_suppressed(
    *,
    net_name: str,
    moment: Union[ConversationMoment, str, None],
    reason: str,
    tenant_id: Any,
    conversation_id: Any,
    customer_phone: Optional[str] = None,
) -> None:
    """Emit the canonical ``[CX] safety_net_suppressed`` line.

    Operators grep this token to answer "why didn't the location
    URL land on this turn?". Keep field names stable — they are
    consumed by the runbook.
    """
    try:
        moment_token = (
            moment.value if isinstance(moment, ConversationMoment) else str(moment or "")
        )
        masked_phone = ""
        if customer_phone:
            try:
                masked_phone = "*" + str(customer_phone)[-4:]
            except Exception:
                masked_phone = ""
        logger.info(
            "[CX] safety_net_suppressed net_name=%s moment=%s reason=%s "
            "tenant_id=%s conversation_id=%s phone=%s",
            net_name, moment_token, reason, tenant_id, conversation_id,
            masked_phone,
        )
    except Exception:
        pass


__all__ = [
    "SUPPRESSIBLE_NETS",
    "NEVER_SUPPRESSIBLE_NETS",
    "is_safety_net_suppression_enabled",
    "should_suppress_safety_net",
    "log_safety_net_suppressed",
]
