"""
services/cart_recovery_failures.py
──────────────────────────────────
Structured failure taxonomy for **send-time** errors in the abandoned-cart
recovery flow (and any other AutomationExecution that ends with the engine
calling ``provider_send_message``).

Why this module exists
──────────────────────
Before this module, three independent failure modes all collapsed into a
single dashboard label of "فشل الإرسال" (send failed):

  1. **Internal short-circuits** in ``automation_engine._execute_action``
     returned dicts like ``{"error": "no_customer_phone"}`` — useful, but
     never localised.
  2. **Send exceptions** were stringified via ``str(exc)[:500]`` —
     leaking Python tracebacks into the dashboard (e.g. ``"'messages'"``
     when Meta returned an error body and the engine tried to read
     ``response["messages"][0]["id"]``).
  3. **Meta error JSONs** returned by the Cloud / 360dialog APIs were
     **not parsed at all**. The engine treated any non-throwing call as
     success, so a template the merchant hadn't gotten approved would
     silently look "sent" with ``wa_message_id=None``.

This module does three things:

  * Exposes a small set of stable internal codes that the dashboard can
    pivot on (``invalid_phone_number``, ``template_not_approved``, …).
  * Maps Meta error code/subcode/message → an internal code + Arabic
    UX label, mirroring the contract of
    ``routers.whatsapp_connect._normalize_meta_error`` but specialised
    for **send** errors (different code set than connect/verify/register).
  * Maps the engine's existing internal short-circuit strings
    (``no_customer_phone``, …) to the same Arabic labels so the
    dashboard never has to render an English token.

The *raw* Meta JSON is always preserved on
``AutomationExecution.action_taken.meta_error`` for engineers, but the
**dashboard only sees the localised label** — same UX contract as the
connect flow.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nahla.cart_recovery_failures")


# ── Stable internal codes ───────────────────────────────────────────────────
#
# Keep this list short and **mutually exclusive**. The dashboard switches
# on these codes; if you split a category, surface a new code and add a
# label below — never silently change what an existing code means.
FAILURE_INVALID_PHONE          = "invalid_phone_number"
FAILURE_MISSING_PHONE          = "missing_phone_number"
FAILURE_NO_CUSTOMER            = "no_customer"
FAILURE_NO_WA_CONNECTION       = "no_whatsapp_connection"
FAILURE_NO_APPROVED_TEMPLATE   = "template_not_approved"
FAILURE_TEMPLATE_PARAM_MISMATCH = "template_param_mismatch"
FAILURE_PROVIDER_AUTH          = "provider_auth_failed"
FAILURE_PROVIDER_TIMEOUT       = "provider_timeout"
FAILURE_PROVIDER_RETURNED_ERROR = "provider_returned_error"
FAILURE_PROVIDER_EMPTY_RESPONSE = "provider_returned_empty_response"
FAILURE_RECEIVER_INCAPABLE     = "receiver_incapable"
FAILURE_RE_ENGAGEMENT_WINDOW   = "re_engagement_window_required"
FAILURE_RATE_LIMITED           = "rate_limited"
FAILURE_SERVICE_DOWN           = "provider_service_down"
FAILURE_AI_DISABLED            = "ai_recovery_disabled"
FAILURE_AI_WINDOW_CLOSED       = "ai_recovery_window_closed"
FAILURE_STEP_DISABLED          = "step_disabled"
FAILURE_UNKNOWN                = "send_failed_unknown"


# ── Arabic UX labels ────────────────────────────────────────────────────────
_LABELS_AR: Dict[str, str] = {
    FAILURE_INVALID_PHONE:           "رقم الجوال غير صالح",
    FAILURE_MISSING_PHONE:           "لا يوجد رقم جوال للعميل",
    FAILURE_NO_CUSTOMER:             "العميل غير مرتبط بالحدث",
    FAILURE_NO_WA_CONNECTION:        "لم يتم ربط واتساب الأعمال",
    FAILURE_NO_APPROVED_TEMPLATE:    "القالب غير معتمد من Meta",
    FAILURE_TEMPLATE_PARAM_MISMATCH: "متغيرات القالب غير مطابقة",
    FAILURE_PROVIDER_AUTH:           "فشل التحقق من بيانات اعتماد واتساب",
    FAILURE_PROVIDER_TIMEOUT:        "انتهت المهلة قبل وصول الرد من واتساب",
    FAILURE_PROVIDER_RETURNED_ERROR: "رفض واتساب الرسالة",
    FAILURE_PROVIDER_EMPTY_RESPONSE: "لم يصل تأكيد إرسال من واتساب",
    FAILURE_RECEIVER_INCAPABLE:      "هذا الرقم لا يستقبل رسائل واتساب الأعمال",
    FAILURE_RE_ENGAGEMENT_WINDOW:    "نافذة 24 ساعة مغلقة — يلزم قالب معتمد",
    FAILURE_RATE_LIMITED:            "تم تجاوز حد الإرسال — حاول لاحقًا",
    FAILURE_SERVICE_DOWN:            "خدمة واتساب غير متاحة مؤقتًا",
    FAILURE_AI_DISABLED:             "خطوة الذكاء الاصطناعي غير مفعّلة",
    FAILURE_AI_WINDOW_CLOSED:        "نافذة الذكاء الاصطناعي مغلقة",
    FAILURE_STEP_DISABLED:           "هذه المرحلة معطّلة في الإعدادات",
    FAILURE_UNKNOWN:                 "فشل الإرسال (سبب غير محدد)",
}


# ── Internal-string → code map ──────────────────────────────────────────────
#
# These are the strings the engine writes today via
# ``return False, {"error": "no_customer_phone"}`` etc.
_INTERNAL_TO_CODE: Dict[str, str] = {
    "no_customer_id":            FAILURE_NO_CUSTOMER,
    "no_customer_phone":         FAILURE_MISSING_PHONE,
    "invalid_phone":             FAILURE_INVALID_PHONE,
    "invalid_phone_number":      FAILURE_INVALID_PHONE,
    "no_whatsapp_connection":    FAILURE_NO_WA_CONNECTION,
    "no_approved_template":      FAILURE_NO_APPROVED_TEMPLATE,
    "step_disabled":             FAILURE_STEP_DISABLED,
    "ai_recovery_disabled":      FAILURE_AI_DISABLED,
    "ai_recovery_window_closed": FAILURE_AI_WINDOW_CLOSED,
}


# ── Meta error code map ────────────────────────────────────────────────────
#
# Send-time codes from the WhatsApp Cloud API. This list intentionally
# focuses on the codes a recovery flow can actually trip — connect /
# register codes live in `_normalize_meta_error` (whatsapp_connect.py).
# Source: developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
_META_CODE_MAP: Dict[int, str] = {
    0:        FAILURE_PROVIDER_RETURNED_ERROR,  # generic "We couldn't deliver"
    1:        FAILURE_PROVIDER_RETURNED_ERROR,  # API service / unknown
    2:        FAILURE_SERVICE_DOWN,             # API service down
    3:        FAILURE_PROVIDER_RETURNED_ERROR,
    4:        FAILURE_RATE_LIMITED,             # API too many calls
    10:       FAILURE_PROVIDER_AUTH,            # permission denied
    100:      FAILURE_TEMPLATE_PARAM_MISMATCH,  # invalid parameter
    131008:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # required parameter missing
    131009:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # parameter value invalid
    131016:   FAILURE_SERVICE_DOWN,             # service unavailable
    131021:   FAILURE_RECEIVER_INCAPABLE,       # recipient cannot be sent to
    131026:   FAILURE_RECEIVER_INCAPABLE,       # message undeliverable
    131031:   FAILURE_PROVIDER_AUTH,            # account locked
    131042:   FAILURE_PROVIDER_AUTH,            # business account restriction
    131047:   FAILURE_RE_ENGAGEMENT_WINDOW,    # re-engagement message
    131048:   FAILURE_RATE_LIMITED,             # spam rate limit
    131049:   FAILURE_RATE_LIMITED,             # marketing limit reached
    131051:   FAILURE_PROVIDER_RETURNED_ERROR,  # unsupported message type
    131056:   FAILURE_RATE_LIMITED,             # pair rate limit
    132000:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # parameter count mismatch
    132001:   FAILURE_NO_APPROVED_TEMPLATE,    # template does not exist
    132005:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # hydrated text too long
    132007:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # template format character policy violated
    132012:   FAILURE_TEMPLATE_PARAM_MISMATCH,  # template parameter format mismatch
    132015:   FAILURE_NO_APPROVED_TEMPLATE,    # template is paused (treat as not-approved for retry)
    132016:   FAILURE_NO_APPROVED_TEMPLATE,    # template is disabled
    133000:   FAILURE_PROVIDER_RETURNED_ERROR,  # decryption error
    190:      FAILURE_PROVIDER_AUTH,            # access token expired
    200:      FAILURE_PROVIDER_AUTH,            # missing permissions
    368:      FAILURE_PROVIDER_AUTH,            # temporarily blocked
}


# ── Public API ──────────────────────────────────────────────────────────────
def label_for_code(code: Optional[str]) -> str:
    """Return the Arabic label for a known internal code, or the
    generic 'send failed' label when the code is missing/unknown."""
    if not code:
        return _LABELS_AR[FAILURE_UNKNOWN]
    return _LABELS_AR.get(code, _LABELS_AR[FAILURE_UNKNOWN])


def classify_internal_error(raw: Optional[str]) -> Tuple[str, str]:
    """
    Map an engine-emitted ``info["error"]`` string (e.g.
    ``"no_customer_phone"``) to ``(internal_code, label_ar)``.

    Unknown / free-form strings collapse to ``FAILURE_UNKNOWN`` rather
    than being echoed back — the dashboard never needs to render an
    English error token.
    """
    if not raw:
        return FAILURE_UNKNOWN, _LABELS_AR[FAILURE_UNKNOWN]
    code = _INTERNAL_TO_CODE.get(str(raw).strip())
    if code is None:
        # Not a known sentinel; treat as "send_failed_unknown" but
        # preserve the raw message in the caller's structured error
        # so engineers still have a breadcrumb.
        return FAILURE_UNKNOWN, _LABELS_AR[FAILURE_UNKNOWN]
    return code, _LABELS_AR[code]


def classify_meta_response(
    response: Optional[Dict[str, Any]],
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """
    Inspect the JSON returned by ``provider_send_message`` for a
    Meta-style error envelope.

    Returns ``None`` when the response looks healthy (no ``error`` key
    AND ``messages[0].id`` is present). Returns a
    ``(internal_code, label_ar, raw_error_dict)`` tuple when the response
    carries an error or is empty.

    Note: an empty / partial response — i.e. neither ``messages[0].id``
    nor ``error`` — is **also** classified as a failure
    (``provider_returned_empty_response``). Pre-fix, the engine treated
    that case as success and persisted ``wa_message_id=None``.
    """
    if not isinstance(response, dict):
        # Most provider failures land here when httpx returned non-JSON or None.
        return (
            FAILURE_PROVIDER_EMPTY_RESPONSE,
            _LABELS_AR[FAILURE_PROVIDER_EMPTY_RESPONSE],
            {"raw": str(response) if response is not None else "null"},
        )

    err = response.get("error")
    if isinstance(err, dict):
        code_raw = err.get("code")
        try:
            code_int = int(code_raw) if code_raw is not None else None
        except (TypeError, ValueError):
            code_int = None
        message = str(err.get("message") or "")
        subcode = err.get("error_subcode")
        try:
            subcode_int = int(subcode) if subcode is not None else None
        except (TypeError, ValueError):
            subcode_int = None

        internal: Optional[str] = None
        if code_int is not None and code_int in _META_CODE_MAP:
            internal = _META_CODE_MAP[code_int]
        if internal is None:
            internal = _heuristic_from_message(message)
        if internal is None:
            internal = FAILURE_PROVIDER_RETURNED_ERROR

        return (
            internal,
            _LABELS_AR.get(internal, _LABELS_AR[FAILURE_UNKNOWN]),
            {
                "code":     code_int,
                "subcode":  subcode_int,
                "message":  message[:500],
                "type":     err.get("type"),
                "trace_id": err.get("fbtrace_id"),
            },
        )

    # No "error" — must have a sent message id to count as success.
    messages = response.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("id"):
            return None  # healthy

    return (
        FAILURE_PROVIDER_EMPTY_RESPONSE,
        _LABELS_AR[FAILURE_PROVIDER_EMPTY_RESPONSE],
        {"raw": _safe_truncate(response, 500)},
    )


def classify_send_exception(exc: BaseException) -> Tuple[str, str, str]:
    """
    Map an exception raised by the provider HTTP layer to
    ``(internal_code, label_ar, raw_message)``.

    We deliberately do **not** unwrap the entire chain — the goal is to
    pick the shortest accurate code, not to faithfully reproduce the
    Python traceback in the merchant's dashboard.
    """
    name = type(exc).__name__
    msg = str(exc)
    raw = f"{name}: {msg}"[:500]

    if "Timeout" in name or "timeout" in msg.lower():
        return FAILURE_PROVIDER_TIMEOUT, _LABELS_AR[FAILURE_PROVIDER_TIMEOUT], raw
    if "ConnectError" in name or "Connect" in name and "Error" in name:
        return FAILURE_SERVICE_DOWN, _LABELS_AR[FAILURE_SERVICE_DOWN], raw
    # Anything else: keep the raw message for engineering, but hide
    # behind the generic label for the merchant.
    return FAILURE_UNKNOWN, _LABELS_AR[FAILURE_UNKNOWN], raw


# ── Internal helpers ────────────────────────────────────────────────────────
def _heuristic_from_message(message: str) -> Optional[str]:
    """Best-effort fallback when Meta returned an error code we haven't
    catalogued. Narrow patterns only — never match "invalid" or "error"
    alone."""
    if not message:
        return None
    m = message.lower()
    if "template" in m and ("not exist" in m or "does not exist" in m or "not found" in m):
        return FAILURE_NO_APPROVED_TEMPLATE
    if "template" in m and ("paused" in m or "disabled" in m or "rejected" in m):
        return FAILURE_NO_APPROVED_TEMPLATE
    if "parameter" in m and ("count" in m or "missing" in m or "format" in m):
        return FAILURE_TEMPLATE_PARAM_MISMATCH
    if "re-engagement" in m or "24 hour" in m or "re engagement" in m:
        return FAILURE_RE_ENGAGEMENT_WINDOW
    if "rate" in m and "limit" in m:
        return FAILURE_RATE_LIMITED
    if "access token" in m and ("expired" in m or "invalid" in m):
        return FAILURE_PROVIDER_AUTH
    if "permission" in m and ("missing" in m or "insufficient" in m):
        return FAILURE_PROVIDER_AUTH
    if "incapable" in m or "cannot receive" in m or "undeliverable" in m:
        return FAILURE_RECEIVER_INCAPABLE
    return None


def _safe_truncate(value: Any, limit: int) -> str:
    try:
        s = str(value)
    except Exception:
        s = repr(value)
    return s[:limit]
