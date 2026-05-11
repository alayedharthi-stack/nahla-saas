"""
backend/services/meta_errors.py
────────────────────────────────
Translate raw WhatsApp Cloud API error responses into a stable,
merchant-friendly classification.

Why this exists
───────────────
Meta returns errors as opaque numeric codes + English-only messages
(e.g. ``{"code": 131026, "message": "Message undeliverable",
"error_subcode": 2494007}``). Surfacing those to the merchant gives
them no idea what happened or what to do — and the campaign reports
were showing strings like ``client_side (meta_error):+9665...`` that
were neither human nor machine-friendly.

This module is the SINGLE source of truth for that mapping. The
campaign dispatcher writes the canonical ``key`` into
``CampaignSendLog.error_code``; the debug endpoint + UI then look up
the Arabic label, severity and recoverability from this table without
any further parsing.

Severity scale
──────────────
* ``minor``     — recipient-specific, doesn't reflect a problem with
                  the campaign or the merchant. The campaign should
                  NOT be marked "failed" if every failure is minor
                  (e.g. "the customer doesn't have WhatsApp").
* ``major``     — fixable per recipient (bad phone, opt-out).
* ``blocking``  — affects the entire campaign (template paused,
                  rate limit, account locked) — if every failure is
                  blocking, the campaign IS failed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("nahla.meta_errors")


@dataclass(frozen=True)
class ClassifiedError:
    """A single classification result.

    The fields are designed to be JSON-serialisable so we can ship
    them directly in /campaigns/{id}/debug without further mapping.

    ``is_recoverable`` and ``retryable`` are intentionally distinct:

    * ``is_recoverable`` answers the merchant's question — "could this
      *ever* succeed?" (e.g. ``rate_limit`` is recoverable: the merchant
      can fix it by waiting; ``not_on_whatsapp`` is NOT recoverable:
      the customer simply doesn't have WhatsApp).

    * ``retryable`` answers the dispatcher's question — "should we
      try the EXACT SAME send again *automatically*?" This is what
      ``reschedule_failed_for_retry`` keys off. A row classified as
      ``client_payment_blocked`` is not retryable (retrying produces
      the same error and burns attempts), even though the merchant
      could in theory "recover" by contacting the customer.
    """
    key:           str          # canonical, machine-stable
    label_ar:      str          # what the merchant sees
    severity:      str          # "minor" | "major" | "blocking"
    is_recoverable: bool         # could the merchant fix this and re-send?
    advice_ar:     Optional[str] = None  # one-line "what to do"
    # Retry policy for the dispatcher. Defaults to False — a class
    # only opts in to auto-retries when we have evidence the same
    # send may succeed without merchant action (rate limits,
    # service_unavailable, transient exceptions).
    retryable:     bool = False


# ──────────────────────────────────────────────────────────────────────
# Canonical error catalogue
# ──────────────────────────────────────────────────────────────────────
#
# Keep this list short and merchant-relevant. We don't try to enumerate
# every Meta error code — anything we can't classify is mapped onto
# ``unknown``, which surfaces as "خطأ من Meta — راجع الدعم" so the
# merchant doesn't see raw English jargon.
ERRORS: Dict[str, ClassifiedError] = {
    "not_on_whatsapp": ClassifiedError(
        key="not_on_whatsapp",
        label_ar="الرقم لا يملك حساب واتساب",
        severity="minor",
        is_recoverable=False,
        retryable=False,
        advice_ar="هذا العميل لا يمكن مراسلته على واتساب — تجاهله أو تواصل عبر قناة أخرى.",
    ),
    "invalid_phone": ClassifiedError(
        key="invalid_phone",
        label_ar="رقم الهاتف غير صالح",
        severity="major",
        is_recoverable=False,
        retryable=False,
        advice_ar="تأكد من صيغة الرقم E.164 (مثال: +9665XXXXXXXX).",
    ),
    "out_of_24h_window": ClassifiedError(
        key="out_of_24h_window",
        label_ar="انتهت نافذة 24 ساعة لخدمة العميل",
        severity="major",
        is_recoverable=False,
        retryable=False,
        advice_ar="استخدم قالب تسويقي معتمد بدل الرسالة الحرة.",
    ),
    "user_not_opted_in": ClassifiedError(
        key="user_not_opted_in",
        label_ar="العميل لم يوافق على استقبال الرسائل التسويقية",
        severity="minor",
        is_recoverable=False,
        retryable=False,
        advice_ar="اطلب موافقة العميل (opt-in) قبل إرسال الحملة.",
    ),
    "marketing_blocked": ClassifiedError(
        key="marketing_blocked",
        label_ar="Meta تمنع الرسائل التسويقية لهذا العميل حالياً",
        severity="minor",
        is_recoverable=True,
        retryable=False,
        advice_ar="حاول لاحقاً — قد تكون Meta أعادت تقييم العميل.",
    ),
    # NEW: Meta returns "This number is blocked due to lack of payment
    # on client side" when the *recipient's* WhatsApp account has been
    # restricted by Meta for billing/payment reasons unrelated to us.
    # Retrying is futile and burns attempts → not retryable.
    # Importantly, this should NOT impact our sender's reputation
    # since the block is entirely on the recipient's side.
    "client_payment_blocked": ClassifiedError(
        key="client_payment_blocked",
        label_ar="الرقم مقيّد من واتساب بسبب مشكلة دفع أو قيود Meta",
        severity="major",
        is_recoverable=False,
        retryable=False,
        advice_ar=(
            "هذه قيود من Meta على حساب العميل ولا يمكن استعادتها من "
            "جانبنا. تخطّى هذا الرقم وتابع — لن يؤثر على سمعة الإرسال."
        ),
    ),
    "rate_limit": ClassifiedError(
        key="rate_limit",
        label_ar="تجاوزت الحصة المسموح بها — انتظر دقيقة",
        severity="blocking",
        is_recoverable=True,
        retryable=True,
        advice_ar="أعد الإرسال بعد بضع دقائق — Meta تطبّق حد رسائل في الدقيقة.",
    ),
    "spam_rate_limit": ClassifiedError(
        key="spam_rate_limit",
        label_ar="حد إرسال الحملات تجاوز السقف اليومي",
        severity="blocking",
        is_recoverable=True,
        retryable=False,  # waiting 24h is a merchant action, not auto-retry
        advice_ar="انتظر 24 ساعة أو ارفع تقييم رقمك لدى Meta.",
    ),
    "template_param_mismatch": ClassifiedError(
        key="template_param_mismatch",
        label_ar="عدد متغيّرات القالب لا يطابق ما اعتمدته Meta",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="افتح القالب وتأكد أن كل {{1}}، {{2}}… ممرَّر بقيمة.",
    ),
    "template_not_found": ClassifiedError(
        key="template_not_found",
        label_ar="القالب غير موجود في Meta",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="أعد مزامنة القوالب — قد يكون القالب مُحذف من Meta.",
    ),
    "template_paused": ClassifiedError(
        key="template_paused",
        label_ar="القالب موقوف من Meta",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="القالب أُوقف بسبب جودة منخفضة — أنشئ نسخة جديدة وقدّمها للاعتماد.",
    ),
    "template_disabled": ClassifiedError(
        key="template_disabled",
        label_ar="القالب معطّل أو مرفوض من Meta",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="استخدم قالباً آخر بحالة APPROVED.",
    ),
    "policy_violation": ClassifiedError(
        key="policy_violation",
        label_ar="مخالفة سياسة Meta — الرسالة مرفوضة",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="راجع نص القالب والصور — قد يحتوي على محتوى ممنوع.",
    ),
    "account_locked": ClassifiedError(
        key="account_locked",
        label_ar="حساب واتساب الأعمال مقيّد من Meta",
        severity="blocking",
        is_recoverable=True,
        retryable=False,  # needs WBM intervention, not blind retry
        advice_ar="راجع تنبيهات WhatsApp Business Manager — قد يطلب التحقق.",
    ),
    "service_unavailable": ClassifiedError(
        key="service_unavailable",
        label_ar="خدمة Meta غير متاحة مؤقتاً",
        severity="blocking",
        is_recoverable=True,
        retryable=True,
        advice_ar="حاول مجدداً بعد دقائق — مشكلة عابرة من Meta.",
    ),
    "media_error": ClassifiedError(
        key="media_error",
        label_ar="فشل تحميل/تنزيل الوسائط في القالب",
        severity="blocking",
        is_recoverable=True,
        retryable=False,  # merchant must fix the media URL first
        advice_ar="تحقق من الصورة/الفيديو في القالب — قد يكون رابطها معطلاً.",
    ),
    "auth_error": ClassifiedError(
        key="auth_error",
        label_ar="مفتاح واتساب غير صالح",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="أعد ربط واتساب الأعمال من إعدادات الاتصال.",
    ),
    "no_message_id": ClassifiedError(
        key="no_message_id",
        label_ar="Meta قبلت الطلب لكن لم تُعد رقم رسالة — فشل غامض",
        severity="major",
        is_recoverable=True,
        retryable=True,
        advice_ar="حاول مجدداً — إن استمر تواصل مع الدعم.",
    ),
    "exception": ClassifiedError(
        key="exception",
        label_ar="حدث خطأ داخلي أثناء الإرسال",
        severity="blocking",
        is_recoverable=True,
        retryable=True,
        advice_ar="حاول الإرسال مجدداً — إن تكرر الخطأ تواصل مع الدعم.",
    ),
    "unknown": ClassifiedError(
        key="unknown",
        label_ar="خطأ غير معروف من Meta",
        severity="major",
        is_recoverable=True,
        # ``unknown`` is the fingerprint-collection bucket. We allow
        # ONE explicit retry (the merchant clicks "أرسل الآن") so the
        # dispatcher gathers a second sample, but the per-row cap
        # MAX_SEND_ATTEMPTS still applies and stops storms.
        retryable=True,
        advice_ar="انسخ الخطأ التقني وأرسله للدعم لتحديد السبب.",
    ),
    # Synthetic terminal codes the dispatcher itself emits — surfaced
    # so the UI can render Arabic labels for them.
    "retry_exhausted": ClassifiedError(
        key="retry_exhausted",
        label_ar="تم إيقاف المحاولات بعد الوصول للحد الأقصى",
        severity="major",
        is_recoverable=False,
        retryable=False,
        advice_ar="راجع آخر خطأ على الصف، أو ابدأ حملة جديدة لمستلمين محددين.",
    ),
    "retry_storm": ClassifiedError(
        key="retry_storm",
        label_ar="تم إيقاف الصف تلقائياً (retry storm)",
        severity="blocking",
        is_recoverable=False,
        retryable=False,
        advice_ar="هذه حالة حماية — تواصل مع الدعم لمراجعة سبب الإرسال المتكرر.",
    ),
    "watchdog_timeout": ClassifiedError(
        key="watchdog_timeout",
        label_ar="فشل بدون رد من Meta لفترة طويلة (watchdog)",
        severity="major",
        is_recoverable=True,
        retryable=True,
        advice_ar="أعد المحاولة — إن تكرر الخطأ تواصل مع الدعم.",
    ),
}


# ──────────────────────────────────────────────────────────────────────
# Classification logic
# ──────────────────────────────────────────────────────────────────────
#
# Meta's error space is messy: the same situation can surface as a
# numeric code on one path and a free-text message on another. We use
# a layered approach:
#
#   1. Match on the numeric ``code`` first — most reliable.
#   2. Fall back to ``error_subcode`` for fine-grained 131xxx splits.
#   3. Last resort: regex over the free-text message (with explicit
#      English keyword fragments Meta is known to emit).

# Code → canonical key. ``None`` keys mean "subcode-only resolution".
#
# Production fingerprints — every code is observed in the wild and
# documented at developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes.
# When you spot a NEW code in Railway logs (search the warning
# "[campaign_dispatcher] campaign=%d Meta error key=unknown"), add it
# here with the most-applicable canonical key.
_CODE_MAP: Dict[int, str] = {
    # ── OAuth / auth errors ──
    0:      "auth_error",
    3:      "auth_error",             # API method permission
    10:     "auth_error",
    190:    "auth_error",
    200:    "auth_error",
    # ── Generic + rate limit ──
    1:      "service_unavailable",
    2:      "service_unavailable",
    4:      "rate_limit",
    17:     "rate_limit",
    32:     "rate_limit",
    100:    "invalid_phone",          # Param to is not a valid phone number
    130429: "rate_limit",
    # ── 131xxx — message delivery ──
    131000: "service_unavailable",
    131005: "auth_error",
    131008: "template_param_mismatch", # required param missing
    131009: "invalid_phone",
    131016: "service_unavailable",
    131021: "invalid_phone",          # recipient cannot be sender
    131026: "not_on_whatsapp",        # message undeliverable
    131031: "account_locked",
    131042: "service_unavailable",    # business eligibility issue
    131045: "template_param_mismatch",
    131047: "out_of_24h_window",
    131048: "spam_rate_limit",
    131049: "marketing_blocked",
    131051: "media_error",
    131052: "media_error",
    131053: "media_error",
    131056: "rate_limit",              # pair rate limit hit
    131057: "service_unavailable",
    131058: "media_error",             # message too long
    # ── 132xxx — template / policy ──
    132000: "template_param_mismatch",
    132001: "template_not_found",
    132005: "template_param_mismatch",
    132007: "policy_violation",
    132012: "template_param_mismatch",
    132015: "template_paused",
    132016: "template_disabled",
    132068: "policy_violation",
    132069: "policy_violation",
    # ── 133xxx — phone / WABA registration ──
    133000: "invalid_phone",
    133004: "service_unavailable",
    133005: "service_unavailable",
    133006: "service_unavailable",
    133008: "service_unavailable",
    133009: "service_unavailable",
    133010: "invalid_phone",
    133012: "invalid_phone",
    133015: "service_unavailable",
    # ── 135xxx — graph + WABA quality ──
    135000: "service_unavailable",
    136025: "policy_violation",
    # ── 368 — temporarily blocked ──
    368:    "policy_violation",
}


# Free-text patterns. The order matters — first match wins.
_TEXT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # NEW: Meta returns this exact English string when the recipient's
    # WhatsApp account is restricted by Meta over billing/payment on
    # their side (entirely outside our control). Production-observed.
    (
        re.compile(
            r"(blocked.*lack.*payment|payment.*client.*side|"
            r"number.*restricted.*billing)",
            re.I,
        ),
        "client_payment_blocked",
    ),
    (re.compile(r"not.*whatsapp", re.I),                 "not_on_whatsapp"),
    (re.compile(r"opted.?out|opt[\-\s]?out|do not contact", re.I), "user_not_opted_in"),
    (re.compile(r"opted.?in|opt[\-\s]?in.*required", re.I), "user_not_opted_in"),
    (re.compile(r"24[\-\s]?hour|service window|re-?engagement", re.I), "out_of_24h_window"),
    (re.compile(r"rate[\-\s]?limit|too many|throttl", re.I), "rate_limit"),
    (re.compile(r"spam", re.I),                          "spam_rate_limit"),
    (re.compile(r"template.*paus", re.I),                "template_paused"),
    (re.compile(r"template.*disabl|template.*reject", re.I), "template_disabled"),
    (re.compile(r"template.*not.*found|missing.*template", re.I), "template_not_found"),
    (re.compile(r"parameter|placeholder|variable.*mismatch", re.I), "template_param_mismatch"),
    (re.compile(r"policy|business.*violat", re.I),       "policy_violation"),
    (re.compile(r"account.*lock|account.*restrict", re.I), "account_locked"),
    (re.compile(r"invalid.*phone|wa[_\-]?id|phone.*format", re.I), "invalid_phone"),
    (re.compile(r"unauth|access.*denied|invalid.*token", re.I), "auth_error"),
    (re.compile(r"unavailable|temporar|try.*again", re.I), "service_unavailable"),
    (re.compile(r"media|image|video|document.*error", re.I), "media_error"),
]


def classify_meta_error(
    *,
    code: Any = None,
    subcode: Any = None,
    error_type: Any = None,
    message: Any = None,
    raw_response: Any = None,
) -> ClassifiedError:
    """Return the best-effort classification of a Meta error.

    All arguments are optional — pass whatever the provider returned.
    The function is total: it ALWAYS returns a ``ClassifiedError``,
    falling back to the ``unknown`` entry as a last resort, so callers
    don't need to defend against ``None``.
    """
    # 1. Numeric code is the most reliable signal.
    try:
        code_int = int(code) if code is not None and str(code).strip() else None
    except (TypeError, ValueError):
        code_int = None
    if code_int is not None and code_int in _CODE_MAP:
        return ERRORS[_CODE_MAP[code_int]]

    # Some 131xxx codes resolve via subcode.
    if code_int == 131047:
        return ERRORS["out_of_24h_window"]

    # 2. Free-text fallback — guard against None / numeric noise.
    msg = str(message or "")
    if msg:
        for pat, key in _TEXT_PATTERNS:
            if pat.search(msg):
                return ERRORS[key]

    # 3. Some legacy errors come through with ``code='exception'`` from
    # our own dispatcher when the asyncio task itself raised — keep
    # that explicit so the merchant doesn't see "Unknown".
    code_str = str(code or "").lower().strip()
    if code_str in ERRORS:
        return ERRORS[code_str]
    if code_str in ("no_message_id", "exception", "auth_error"):
        return ERRORS[code_str]

    return ERRORS["unknown"]


def format_technical(
    *,
    code: Any = None,
    subcode: Any = None,
    error_type: Any = None,
    message: Any = None,
) -> str:
    """Canonical one-line technical string we store in
    ``CampaignSendLog.error_message`` and surface verbatim in the UI
    when the classifier falls back to ``unknown``. Stable format so
    support can grep production logs deterministically."""
    msg = str(message or "Unknown Meta error").strip()
    code_part = f"code={code}" if code is not None and str(code).strip() else "code=?"
    sub_part = (
        f" subcode={subcode}"
        if subcode is not None and str(subcode).strip()
        else ""
    )
    type_part = (
        f" type={error_type}"
        if error_type is not None and str(error_type).strip()
        else ""
    )
    return f"[{code_part}{sub_part}{type_part}] {msg}"


def parse_technical(text: Optional[str]) -> Dict[str, Optional[str]]:
    """Inverse of ``format_technical``. Given a stored
    ``CampaignSendLog.error_message`` string (or any other string of
    the same shape), return the parsed component fields. Never raises
    — returns whatever it can recover, falling back to ``None`` for
    missing fields so the UI can still render a partial breakdown."""
    out: Dict[str, Optional[str]] = {
        "meta_error_code":    None,
        "meta_error_subcode": None,
        "meta_error_type":    None,
        "meta_error_message": None,
    }
    if not text:
        return out
    s = str(text)
    head_match = re.match(r"^\s*\[([^\]]*)\]\s*(.*)$", s, re.S)
    if not head_match:
        out["meta_error_message"] = s.strip() or None
        return out
    head, body = head_match.group(1), head_match.group(2)
    out["meta_error_message"] = body.strip() or None
    for tok in re.findall(r"(code|subcode|type)\s*=\s*([^\s\]]+)", head, re.I):
        key, val = tok[0].lower(), tok[1]
        if val.lower() in ("none", "null", "?"):
            continue
        if key == "code":
            out["meta_error_code"] = val
        elif key == "subcode":
            out["meta_error_subcode"] = val
        elif key == "type":
            out["meta_error_type"] = val
    return out


def severity_of(key: str) -> str:
    """Quick lookup — used by the lifecycle classifier to decide if a
    failure is "minor" enough that the campaign shouldn't be marked
    failed-overall."""
    return ERRORS.get(key, ERRORS["unknown"]).severity


def label_for(key: str) -> str:
    """Convenience: Arabic label for a stored error_code."""
    return ERRORS.get(key, ERRORS["unknown"]).label_ar


# ──────────────────────────────────────────────────────────────────────
# Unknown-code registry
# ──────────────────────────────────────────────────────────────────────
#
# Production keeps surfacing new Meta error codes we haven't classified
# yet. We record each new ``(code, subcode)`` tuple **once per process**
# and emit a single structured WARNING line so operators can grep
# Railway logs for ``Unknown Meta code encountered`` and extend
# ``_CODE_MAP`` confidently.
_SEEN_UNKNOWN_KEYS: Set[Tuple[str, str]] = set()


def note_unknown_code(
    *,
    code: Any,
    subcode: Any = None,
    error_type: Any = None,
    message: Any = None,
) -> bool:
    """Record an unknown Meta error code (idempotent per process).

    Returns ``True`` if this is the first time the (code, subcode) tuple
    has been seen this process — handy for tests / metrics.
    """
    code_str = str(code) if code is not None else ""
    sub_str = str(subcode) if subcode is not None else ""
    key = (code_str.strip(), sub_str.strip())
    if not any(key):
        # Pure free-text message — fingerprint by the first 80 chars.
        key = ("", (str(message or "").strip())[:80])
    if key in _SEEN_UNKNOWN_KEYS:
        return False
    _SEEN_UNKNOWN_KEYS.add(key)
    logger.warning(
        "Unknown Meta code encountered code=%s subcode=%s type=%s msg=%s",
        code, subcode, error_type, (str(message or "")[:240]),
    )
    return True


def reset_unknown_registry() -> None:
    """Clear the in-process unknown-code registry. Used by tests."""
    _SEEN_UNKNOWN_KEYS.clear()


def to_dict(c: ClassifiedError) -> Dict[str, Any]:
    """Serialise a ClassifiedError for JSON responses."""
    return {
        "key":            c.key,
        "label_ar":       c.label_ar,
        "severity":       c.severity,
        "is_recoverable": c.is_recoverable,
        "retryable":      c.retryable,
        "advice_ar":      c.advice_ar,
    }


def is_retryable(key: Optional[str]) -> bool:
    """Quick lookup for the dispatcher: should we auto-retry rows
    that failed with this canonical ``error_code``? Defaults to
    False for unknown keys so we stay on the safe side and never
    accidentally storm-retry an unclassified error."""
    if not key:
        return False
    entry = ERRORS.get(str(key).strip().lower())
    return bool(entry.retryable) if entry else False


__all__ = [
    "ClassifiedError",
    "ERRORS",
    "classify_meta_error",
    "format_technical",
    "parse_technical",
    "severity_of",
    "label_for",
    "is_retryable",
    "to_dict",
    "note_unknown_code",
    "reset_unknown_registry",
]
