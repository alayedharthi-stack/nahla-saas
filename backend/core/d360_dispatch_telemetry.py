"""
core/d360_dispatch_telemetry.py
───────────────────────────────
Wave 2.0 Phase 1.5 (May 2026) — narrow 360dialog dispatch-gap probe.

Why this module exists
──────────────────────
W2.0.1 added the ``[INBOUND_LIFECYCLE]`` summary line that wraps every
inbound that reaches ``_dispatch_message``. Three production cases
(``*2692`` video, ``*8626`` and ``*5699`` document) confirmed the
opposite class of failure: the message exists in WhatsApp but **never
appears in the lifecycle trace at all** — meaning it never even
entered ``_dispatch_message``.

The hypothesis is one of:

  (a) 360dialog delivered the inbound under a non-``messages`` field
      (``smb_message_echoes``, ``coexistence``, ``device_sync``,
      mislabelled status), so our field-routing skipped it.
  (b) The change carried a non-empty ``messages[]`` AND a
      non-``messages`` field — the field-routing decided by name and
      ignored the array.
  (c) An early routing gate (missing / unknown / ambiguous
      ``phone_number_id``, bad coexistence secret, wrong-provider
      mismatch, scope mismatch) returned ``continue`` while the same
      change had ``msgs_count > 0``.
  (d) A new field name we don't recognize landed in
      ``[Webhook360] Ignored field=…``.

W2.0.1's ``[INBOUND_LIFECYCLE]`` cannot answer this — it only fires
once we've already entered the per-message context manager. We need a
sibling probe at the **batch / change level**, before any routing
decision, with two grep-able prefixes:

  ``[D360_RAW_INBOUND]``  — once per change, at the top of the loop.
                            Surfaces the raw shape (field, msgs_count,
                            statuses_count, echoes_count, first
                            sender, first message ids) BEFORE any
                            routing happens.

  ``[D360_DISPATCH_GAP]`` — emitted only when ``msgs_count > 0`` AND
                            the routing branch is about to ``continue``
                            without dispatching the messages. The
                            ``reason`` token classifies the gap so
                            operators can split:
                              * messages_in_payload_but_<gate>
                              * messages_in_payload_but_field_<X>
                            and answer "did the customer's media
                            arrive at all from 360dialog?".

  ``[D360_BRANCH]``       — one line per routing decision so a single
                            grep on a masked sender shows which
                            branch handled (or skipped) a given
                            change. Cheap and useful for forensics.

Architectural rules
───────────────────
1. **Telemetry only.** No state writes, no behavioural change. Every
   helper is wrapped so a recording bug can never propagate.
2. **No coupling.** Imports nothing from routers / webhook /
   persistence layers. One-way wiring.
3. **PII safe.** Phone numbers masked to last-4. Body text never
   recorded (length only). Caption text never recorded.
4. **Cheap.** Each helper formats one ``logger.info`` line.
5. **Kill switch.** Same env flag as W2.0.1
   (``INBOUND_LIFECYCLE_TELEMETRY_ENABLED``) so operators can flip
   the entire Wave 2.0 telemetry family OFF in one toggle. A separate
   knob ``D360_DISPATCH_GAP_TELEMETRY_ENABLED`` exists for the rare
   case where ``[INBOUND_LIFECYCLE]`` should remain on but this
   probe alone needs muting.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, List, Mapping, Optional, Sequence

logger = logging.getLogger("nahla.d360_dispatch_telemetry")


# ── Kill switch ────────────────────────────────────────────────────


def _truthy(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "off")


def is_d360_dispatch_telemetry_enabled() -> bool:
    """Resolve the effective kill switch.

    Default: ON (telemetry-only, no behavioural risk).

    Two overrides, read in order:

      1. ``D360_DISPATCH_GAP_TELEMETRY_ENABLED`` — narrow per-probe
         knob. Falsy disables this module specifically.
      2. ``INBOUND_LIFECYCLE_TELEMETRY_ENABLED`` — family-wide knob
         shared with W2.0.1. Falsy disables the entire Wave 2.0
         telemetry family. Operators expect flipping this single
         flag to mute all observation-only logs.

    Both default to ON when unset.
    """
    narrow = os.getenv("D360_DISPATCH_GAP_TELEMETRY_ENABLED", "")
    if narrow and not _truthy(narrow):
        return False
    family = os.getenv("INBOUND_LIFECYCLE_TELEMETRY_ENABLED", "")
    if family and not _truthy(family):
        return False
    return True


# ── Closed reason vocabulary (greppable) ───────────────────────────
# Kept narrow on purpose — adding a new reason is a deliberate change
# tracked by the test suite. ``messages_in_payload_*`` is the prefix
# that matters: every reason where we observed a non-empty
# ``messages[]`` array but did NOT hand it to ``_dispatch_message``.

REASON_MISSING_PHONE_ID         = "messages_in_payload_but_missing_phone_id"
REASON_UNKNOWN_PHONE_ID         = "messages_in_payload_but_unknown_phone_id"
REASON_AMBIGUOUS_PHONE_ID       = "messages_in_payload_but_ambiguous_phone_id"
REASON_WRONG_PROVIDER           = "messages_in_payload_but_wrong_provider"
REASON_BAD_SECRET               = "messages_in_payload_but_bad_secret"
REASON_SCOPE_MISMATCH           = "messages_in_payload_but_scope_mismatch"
REASON_FIELD_NOT_MESSAGES       = "messages_in_payload_but_field_not_messages"
REASON_FIELD_IGNORED            = "messages_in_payload_but_field_ignored"
REASON_DISPATCH_NOT_ENTERED     = "messages_in_payload_but_dispatch_not_entered"

ALL_GAP_REASONS: tuple = (
    REASON_MISSING_PHONE_ID,
    REASON_UNKNOWN_PHONE_ID,
    REASON_AMBIGUOUS_PHONE_ID,
    REASON_WRONG_PROVIDER,
    REASON_BAD_SECRET,
    REASON_SCOPE_MISMATCH,
    REASON_FIELD_NOT_MESSAGES,
    REASON_FIELD_IGNORED,
    REASON_DISPATCH_NOT_ENTERED,
)


# Branch-decision tokens — grep-friendly bucket names for routing
# decisions. Mirrors the conditional structure inside
# ``_handle_360dialog_body``.

BRANCH_MESSAGES                 = "messages"
BRANCH_SMB_MESSAGE_ECHOES       = "smb_message_echoes"
BRANCH_COEXISTENCE              = "coexistence"
BRANCH_STATUS                   = "status"
BRANCH_IGNORED                  = "ignored"
BRANCH_SCOPE_MISMATCH           = "scope_mismatch"
BRANCH_MISSING_PHONE_ID         = "missing_phone_id"
BRANCH_UNKNOWN_PHONE_ID         = "unknown_phone_id"
BRANCH_AMBIGUOUS_PHONE_ID       = "ambiguous_phone_id"
BRANCH_WRONG_PROVIDER           = "wrong_provider"
BRANCH_BAD_SECRET               = "bad_secret"

ALL_BRANCHES: tuple = (
    BRANCH_MESSAGES,
    BRANCH_SMB_MESSAGE_ECHOES,
    BRANCH_COEXISTENCE,
    BRANCH_STATUS,
    BRANCH_IGNORED,
    BRANCH_SCOPE_MISMATCH,
    BRANCH_MISSING_PHONE_ID,
    BRANCH_UNKNOWN_PHONE_ID,
    BRANCH_AMBIGUOUS_PHONE_ID,
    BRANCH_WRONG_PROVIDER,
    BRANCH_BAD_SECRET,
)


# ── Helpers ────────────────────────────────────────────────────────


def _mask_phone(phone: Optional[str]) -> str:
    """Mask a phone to ``*<last-4>``. Empty / non-string input → ``-``.

    Identical contract to ``core.inbound_lifecycle._mask_phone`` so a
    grep across both prefixes lines up on the same masked token.
    """
    try:
        if not phone:
            return "-"
        s = str(phone)
        if len(s) <= 4:
            return "*" + s
        return "*" + s[-4:]
    except Exception:
        return "-"


def _summarize_first_message(messages: Optional[Sequence[Mapping[str, Any]]]) -> dict:
    """Return ``{first_msg_type, first_sender_masked, first_msg_id}``
    derived from ``messages[0]``. Missing / malformed input yields
    ``-`` for each field. Never raises."""
    out = {
        "first_msg_type": "-",
        "first_sender_masked": "-",
        "first_msg_id": "-",
    }
    try:
        if not messages:
            return out
        first = messages[0]
        if not isinstance(first, Mapping):
            return out
        out["first_msg_type"] = str(first.get("type") or "-")
        out["first_sender_masked"] = _mask_phone(first.get("from") or "")
        out["first_msg_id"] = str(first.get("id") or "-")
    except Exception:
        pass
    return out


def _message_ids_tail(
    messages: Optional[Sequence[Mapping[str, Any]]], *, cap: int = 5,
) -> str:
    """Return a comma-joined tail of ``messages[*].id`` (last ``cap``
    entries). Empty input → ``-``. Never raises."""
    try:
        if not messages:
            return "-"
        ids: List[str] = []
        for m in messages:
            try:
                if isinstance(m, Mapping):
                    mid = str(m.get("id") or "")
                    if mid:
                        ids.append(mid)
            except Exception:
                continue
        if not ids:
            return "-"
        tail = ids[-cap:]
        prefix = "..." if len(ids) > cap else ""
        return prefix + ",".join(tail)
    except Exception:
        return "-"


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


# ── Public emitters ────────────────────────────────────────────────


def emit_raw_inbound(
    *,
    scope: str,
    field: str,
    phone_number_id: str,
    msgs_count: int,
    statuses_count: int,
    echoes_count: int,
    messages: Optional[Sequence[Mapping[str, Any]]] = None,
    has_messages_key: Optional[bool] = None,
    has_message_echoes_key: Optional[bool] = None,
    has_statuses_key: Optional[bool] = None,
    entry_idx: Optional[int] = None,
    change_idx: Optional[int] = None,
) -> None:
    """Emit ``[D360_RAW_INBOUND]`` once per ``(entry, change)`` at the
    top of the routing loop, BEFORE any tenant lookup or routing
    branch fires.

    The line is the canonical answer to "did 360dialog deliver this
    customer's media as a customer message?" — a single grep on the
    masked sender shows the exact field, the array sizes, and the
    first message id, regardless of whether the routing later
    accepted or skipped the change.

    Never raises; never logs PII (phone numbers masked to last-4,
    body text not recorded).
    """
    if not is_d360_dispatch_telemetry_enabled():
        return
    try:
        first = _summarize_first_message(messages)
        ids_tail = _message_ids_tail(messages)
        logger.info(
            "[D360_RAW_INBOUND] scope=%s field=%s phone_id=%s "
            "msgs=%d statuses=%d echoes=%d "
            "first_msg_type=%s first_sender_masked=%s "
            "first_msg_id=%s message_ids_tail=%s "
            "has_messages_key=%s has_message_echoes_key=%s "
            "has_statuses_key=%s entry_idx=%s change_idx=%s",
            scope or "-",
            field or "-",
            phone_number_id or "-",
            _safe_int(msgs_count),
            _safe_int(statuses_count),
            _safe_int(echoes_count),
            first["first_msg_type"],
            first["first_sender_masked"],
            first["first_msg_id"],
            ids_tail,
            "" if has_messages_key is None else (
                "true" if has_messages_key else "false"
            ),
            "" if has_message_echoes_key is None else (
                "true" if has_message_echoes_key else "false"
            ),
            "" if has_statuses_key is None else (
                "true" if has_statuses_key else "false"
            ),
            "" if entry_idx is None else int(entry_idx),
            "" if change_idx is None else int(change_idx),
        )
    except Exception:
        pass


def emit_dispatch_gap(
    *,
    reason: str,
    scope: str,
    field: str,
    phone_number_id: str,
    msgs_count: int,
    messages: Optional[Sequence[Mapping[str, Any]]] = None,
    matched_tenant_id: Any = None,
    family: str = "",
    detail: str = "",
) -> None:
    """Emit ``[D360_DISPATCH_GAP]`` when a change carrying a non-empty
    ``messages[]`` array is about to ``continue`` without entering
    ``_dispatch_message``.

    NO-OP when ``msgs_count <= 0`` — a delivery that legitimately
    only carries statuses or echoes is not a gap.

    The ``reason`` token MUST be one of :data:`ALL_GAP_REASONS`. The
    test suite enforces this; an unknown reason is still emitted so
    the production line is never lost, but the test will fail to
    catch the drift.
    """
    if not is_d360_dispatch_telemetry_enabled():
        return
    if _safe_int(msgs_count) <= 0:
        return
    try:
        first = _summarize_first_message(messages)
        ids_tail = _message_ids_tail(messages)
        logger.info(
            "[D360_DISPATCH_GAP] reason=%s scope=%s field=%s family=%s "
            "phone_id=%s msgs=%d "
            "first_msg_type=%s first_sender_masked=%s "
            "first_msg_id=%s message_ids_tail=%s "
            "matched_tenant=%s detail=%s",
            reason or "-",
            scope or "-",
            field or "-",
            family or "-",
            phone_number_id or "-",
            _safe_int(msgs_count),
            first["first_msg_type"],
            first["first_sender_masked"],
            first["first_msg_id"],
            ids_tail,
            matched_tenant_id if matched_tenant_id not in (None, "") else "-",
            detail or "-",
        )
    except Exception:
        pass


def emit_branch_decision(
    *,
    branch: str,
    scope: str,
    field: str,
    family: str = "",
    phone_number_id: str = "",
    msgs_count: int = 0,
    statuses_count: int = 0,
    echoes_count: int = 0,
    messages: Optional[Sequence[Mapping[str, Any]]] = None,
    matched_tenant_id: Any = None,
) -> None:
    """Emit ``[D360_BRANCH]`` once per routing decision so a single
    grep on a masked sender (e.g. ``*2692``) shows which branch
    handled the change — even if the dispatch never followed because
    the field was wrong or the gate rejected it."""
    if not is_d360_dispatch_telemetry_enabled():
        return
    try:
        first = _summarize_first_message(messages)
        logger.info(
            "[D360_BRANCH] decision=%s scope=%s field=%s family=%s "
            "phone_id=%s msgs=%d statuses=%d echoes=%d "
            "first_sender_masked=%s matched_tenant=%s",
            branch or "-",
            scope or "-",
            field or "-",
            family or "-",
            phone_number_id or "-",
            _safe_int(msgs_count),
            _safe_int(statuses_count),
            _safe_int(echoes_count),
            first["first_sender_masked"],
            matched_tenant_id if matched_tenant_id not in (None, "") else "-",
        )
    except Exception:
        pass


__all__ = [
    "is_d360_dispatch_telemetry_enabled",
    "emit_raw_inbound",
    "emit_dispatch_gap",
    "emit_branch_decision",
    # reason tokens
    "REASON_MISSING_PHONE_ID",
    "REASON_UNKNOWN_PHONE_ID",
    "REASON_AMBIGUOUS_PHONE_ID",
    "REASON_WRONG_PROVIDER",
    "REASON_BAD_SECRET",
    "REASON_SCOPE_MISMATCH",
    "REASON_FIELD_NOT_MESSAGES",
    "REASON_FIELD_IGNORED",
    "REASON_DISPATCH_NOT_ENTERED",
    "ALL_GAP_REASONS",
    # branch tokens
    "BRANCH_MESSAGES",
    "BRANCH_SMB_MESSAGE_ECHOES",
    "BRANCH_COEXISTENCE",
    "BRANCH_STATUS",
    "BRANCH_IGNORED",
    "BRANCH_SCOPE_MISMATCH",
    "BRANCH_MISSING_PHONE_ID",
    "BRANCH_UNKNOWN_PHONE_ID",
    "BRANCH_AMBIGUOUS_PHONE_ID",
    "BRANCH_WRONG_PROVIDER",
    "BRANCH_BAD_SECRET",
    "ALL_BRANCHES",
]
