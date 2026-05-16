"""
services/turn_trace.py
──────────────────────
TurnTrace — single structured observability record per inbound message.

Root motivation
───────────────
A merchant reported (May 2026): a customer asked an informational
question — "وشلون طريقة توصيل الطلبات عندكم" — and instead of getting
an answer, the system replied with the canned handoff template
"وصلت رسالتك ✅ سيتم الرد عليك في أقرب وقت من فريق المتجر."

Forensic analysis showed the Brain pipeline crashed and the
``except`` arm in ``whatsapp_webhook.py`` fired the canned safe-reply.
The crash was logged as a one-line ``logger.error("...: %s", exc)``
WITHOUT a traceback, so we never knew what actually broke inside the
Brain. Worse, the canned text told the customer a human will reply —
which is a lie for most tenants that don't have a live agent.

The pattern of "Brain silently fell through to canned-handoff" is
hard to detect from logs because the failure mode is split across
ten different code paths:

    pause_guard  →  mode_resolver  →  trial_guard  →  brain.process
                                                        │
                                                        ├─ silent reply  → canned_ack #1
                                                        ├─ exception     → canned_handoff #2
                                                        └─ welcome_gate  → canned_intro
                                                              │
                                                              ▼
                                           composer  →  outbound  →  send

Without a single per-turn observability record, the merchant team had
to grep across BRAIN_IN / BRAIN_RESULT / WELCOME_GATE_INVALID_REPLY /
BRAIN_SILENT_REPLY / handful-of-OUTBOUND emit sites just to
reconstruct one turn. TurnTrace fixes that.

Contract
────────
ONE inbound turn → ONE ``TurnTrace`` instance → ONE ``[TURN]`` log
line at the end of processing. Every stage of the pipeline updates
fields on the trace; the emit happens in a ``finally`` block in the
webhook handler so a half-processed turn still produces a record.

Fields are JSON-friendly + space-separated key=value-encoded so the
existing structured-log pipeline can ingest the line without a parser
change. ``reply_source`` is the headline value: it tells you whether
the customer got a real LLM answer ("brain"), an early reply
("welcome_gate"), or a canned ack ("brain_exception" / "brain_silent"
/ "outer_exception" / "soft_retry"). Filtering on
``reply_source!=brain`` answers the question "how often do real
customers get canned answers?" — which is the bug class this module
exists to surface.

Design constraints
──────────────────
* Pure-Python, no DB, no I/O at construction time. Only ``emit()``
  performs an I/O (a single ``logger.info``).
* Never raise inside ``emit()`` — observability MUST NOT take down
  the response path.
* Idempotent ``mark_outbound_sent()`` — multiple call sites can
  invoke it; only the first take effect for that turn.
* ``outbound_sent`` flag is a defensive guard against double-send.
  Any code path that wants to send a fallback can check
  ``trace.outbound_sent`` and skip — this is the
  "إذا تم إرسال outbound reply بنجاح، فيجب إلغاء أي pending auto_ack
  لنفس turn/message_id" rule.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical reply-source values — pinned strings so dashboards can filter
# ─────────────────────────────────────────────────────────────────────────────
#
# We avoid free-form strings here because the merchant-side ops team
# already builds Grafana panels off ``[TURN]`` log lines. Adding a new
# source = adding one constant + one assertion in the test suite.

SOURCE_BRAIN              = "brain"               # real LLM answer
SOURCE_WELCOME_GATE       = "welcome_gate"        # early-stage greet substitute
SOURCE_BRAIN_SILENT_ACK   = "brain_silent_ack"    # Brain produced empty reply
SOURCE_BRAIN_EXCEPTION    = "brain_exception"     # Brain raised — canned ack
SOURCE_OUTER_EXCEPTION    = "outer_exception"     # outer try/except — canned ack
SOURCE_SUPPORT_ESCALATION = "support_escalation"  # mode resolver routed to handoff
SOURCE_LEGACY_NO_KEY      = "legacy_no_api_key"   # ANTHROPIC_API_KEY missing
SOURCE_LEGACY             = "legacy"              # legacy generate_ai_reply path
SOURCE_HANDOFF_ACK        = "handoff_ack"         # explicit customer handoff request
SOURCE_PAUSED             = "paused"              # ai_pause_guard skipped this turn
SOURCE_BILLING_DENIED     = "billing_access_denied"
SOURCE_UNKNOWN            = "unknown"


_ALL_SOURCES = {
    SOURCE_BRAIN, SOURCE_WELCOME_GATE, SOURCE_BRAIN_SILENT_ACK,
    SOURCE_BRAIN_EXCEPTION, SOURCE_OUTER_EXCEPTION,
    SOURCE_SUPPORT_ESCALATION, SOURCE_LEGACY_NO_KEY, SOURCE_LEGACY,
    SOURCE_HANDOFF_ACK, SOURCE_PAUSED, SOURCE_BILLING_DENIED,
    SOURCE_UNKNOWN,
}


# ─────────────────────────────────────────────────────────────────────────────
# Canonical delivery-mode values
# ─────────────────────────────────────────────────────────────────────────────

DELIVERY_TEXT          = "text"            # plain text outbound
DELIVERY_INTERACTIVE   = "interactive"     # buttons / list
DELIVERY_PRODUCT_CARD  = "product_card"
DELIVERY_MEDIA         = "media"
DELIVERY_NONE          = "none"            # nothing was sent


# ─────────────────────────────────────────────────────────────────────────────
# Trace dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TurnTrace:
    """One inbound message → one trace.

    The trace is INSTANTIATED at the top of the webhook handler
    (right after we have ``tenant_id`` + ``phone`` + ``message_id``)
    and EMITTED in a ``finally`` block at the bottom. In between,
    every layer (pause guard / mode resolver / brain / composer /
    sender / fallback) mutates the trace by writing to attributes
    or calling helper methods.

    Attributes are deliberately wide and flat — every field that
    might matter for debugging is exposed at the top level so the
    ``[TURN]`` log line is greppable without JSON parsing. Lists are
    used only for ``missing_fields`` and ``options_pending`` because
    those genuinely vary in length.
    """

    # ── Identity ─────────────────────────────────────────────────
    tenant_id:        int
    phone:            str
    message_id:       str = ""              # inbound wamid (Meta's `messages[0].id`)
    inbound_text:     str = ""              # truncated for log
    started_at_ms:    int = field(default_factory=lambda: int(time.time() * 1000))

    # ── Stage 1: routing decisions BEFORE the Brain ──────────────
    paused:           bool = False          # ai_pause_guard.should_skip_ai
    pause_reason:     str  = ""
    mode:             str  = ""             # resolve_conversation_mode() output
    intent:           str  = ""             # intent.rules.match() name, if any
    stance:           str  = ""             # NEW: classifies turn purpose (informational / order / handoff / smalltalk)

    # ── Stage 2: Brain pipeline ──────────────────────────────────
    brain_called:     bool = False
    brain_action:     str  = ""             # last_action from brain_state
    brain_stage:      str  = ""             # stage from brain_state
    brain_failed:     bool = False          # raised an exception
    brain_silent:     bool = False          # returned empty reply
    brain_exc_class:  str  = ""             # exception class name (no stack — logged separately)

    # ── Stage 3: response synthesis ──────────────────────────────
    response_goal:    str  = ""             # what we INTENDED to do (answer / handoff / ack / silence)
    delivery_mode:    str  = DELIVERY_NONE
    fallback_source:  str  = ""             # if non-empty, the chosen fallback class

    # ── Stage 4: final outbound ──────────────────────────────────
    reply_source:     str  = SOURCE_UNKNOWN
    reply_len:        int  = 0
    buttons_count:    int  = 0
    handoff_triggered:bool = False
    outbound_sent:    bool = False
    outbound_error:   str  = ""

    # ── Side info (lists kept short) ─────────────────────────────
    missing_fields:   list[str] = field(default_factory=list)
    options_pending:  list[str] = field(default_factory=list)
    extra:            dict      = field(default_factory=dict)

    # ── Helpers ──────────────────────────────────────────────────

    def mark_outbound_sent(self, *, source: str, length: int = 0, mode: str = DELIVERY_TEXT) -> None:
        """Idempotent — first caller wins.

        Any subsequent caller (e.g. a fallback handler that fires AFTER
        a real reply has been sent) gets ``False`` from
        :meth:`outbound_lock_acquired` and should bail out. This is
        the defence-in-depth implementation of the merchant's rule:

            "إذا تم إرسال outbound reply بنجاح، فيجب إلغاء أي pending
             auto_ack لنفس turn/message_id."

        The flag is a process-local boolean — across multiple workers
        a true second send would still be possible, but in practice
        we only emit fallbacks inside the same coroutine as the
        primary reply, so a single-flag guard catches every internal
        regression.
        """
        if self.outbound_sent:
            return
        self.outbound_sent  = True
        self.reply_source   = source
        self.reply_len      = int(length or 0)
        self.delivery_mode  = mode

    def outbound_lock_acquired(self) -> bool:
        """Return True iff this caller is the first to mark outbound.

        Use BEFORE a fallback send:

            if not trace.outbound_lock_acquired():
                # another path already sent a reply — do not double-send
                return
        """
        return not self.outbound_sent

    def mark_brain_exception(self, exc: BaseException) -> None:
        self.brain_failed    = True
        self.brain_exc_class = exc.__class__.__name__

    # ── Emit ─────────────────────────────────────────────────────

    def emit(self) -> None:
        """Single structured log line summarising the turn.

        Never raises. Logged at INFO so the line lands in the
        default Railway log stream. Filtering example:

            $ railway logs | rg '\\[TURN\\] .* reply_source=brain_exception'

        would return every customer who got a canned safe-reply
        because Brain crashed — the exact bug class this module
        exists to surface.
        """
        try:
            elapsed = max(0, int(time.time() * 1000) - int(self.started_at_ms))
            # Truncate inbound to a reasonable log size — long messages
            # would crowd the line and reduce greppability.
            inbound = (self.inbound_text or "").replace("\n", " ")[:160]
            # Validate reply_source defensively — a typo upstream
            # would land here as garbage; downgrade to ``unknown``
            # and append a hint so we can find the offending site.
            src = self.reply_source if self.reply_source in _ALL_SOURCES else SOURCE_UNKNOWN
            invalid_src_hint = "" if src == self.reply_source else f" invalid_reply_source={self.reply_source!r}"

            logger.info(
                "[TURN] tenant=%s phone=*%s wamid=%s "
                "mode=%s intent=%s stance=%s "
                "paused=%s pause_reason=%s "
                "brain_called=%s brain_failed=%s brain_silent=%s "
                "brain_action=%s brain_stage=%s brain_exc=%s "
                "response_goal=%s delivery=%s fallback_source=%s "
                "reply_source=%s reply_len=%d buttons=%d "
                "handoff=%s outbound_sent=%s outbound_err=%s "
                "missing=%s opts=%s elapsed_ms=%d inbound=%r%s",
                self.tenant_id, (self.phone or "")[-4:], self.message_id or "-",
                self.mode or "-", self.intent or "-", self.stance or "-",
                self.paused, self.pause_reason or "-",
                self.brain_called, self.brain_failed, self.brain_silent,
                self.brain_action or "-", self.brain_stage or "-", self.brain_exc_class or "-",
                self.response_goal or "-", self.delivery_mode or "-", self.fallback_source or "-",
                src, self.reply_len, self.buttons_count,
                self.handoff_triggered, self.outbound_sent, self.outbound_error or "-",
                self.missing_fields, self.options_pending, elapsed, inbound, invalid_src_hint,
            )
        except Exception as _emit_exc:  # noqa: BLE001
            # Observability MUST NOT crash the response path. Last-ditch
            # bare logger so we still leave a breadcrumb.
            try:
                logger.error("[TURN_EMIT_FAILED] %s", _emit_exc)
            except Exception:  # noqa: BLE001
                pass


def new_trace(*, tenant_id: int, phone: str, message_id: str = "", inbound_text: str = "") -> TurnTrace:
    """Public constructor. Use at the top of the webhook handler.

    Equivalent to ``TurnTrace(...)`` but the function form makes
    monkey-patching easier in tests (replace ``new_trace`` to inject
    a recording double).
    """
    return TurnTrace(
        tenant_id    = int(tenant_id),
        phone        = phone or "",
        message_id   = message_id or "",
        inbound_text = (inbound_text or "")[:240],
    )


__all__ = [
    "DELIVERY_INTERACTIVE",
    "DELIVERY_MEDIA",
    "DELIVERY_NONE",
    "DELIVERY_PRODUCT_CARD",
    "DELIVERY_TEXT",
    "SOURCE_BILLING_DENIED",
    "SOURCE_BRAIN",
    "SOURCE_BRAIN_EXCEPTION",
    "SOURCE_BRAIN_SILENT_ACK",
    "SOURCE_HANDOFF_ACK",
    "SOURCE_LEGACY",
    "SOURCE_LEGACY_NO_KEY",
    "SOURCE_OUTER_EXCEPTION",
    "SOURCE_PAUSED",
    "SOURCE_SUPPORT_ESCALATION",
    "SOURCE_UNKNOWN",
    "SOURCE_WELCOME_GATE",
    "TurnTrace",
    "new_trace",
]
