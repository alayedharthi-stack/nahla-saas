"""The merchant's Arabic-dialect setting and what the platform means by each choice.

``ai_settings.arabic_dialect`` is independent of the reply language
(``ai_settings.default_language``): a merchant may pair any language choice with
any dialect. The dialect governs how an Arabic reply is written; the language
setting alone decides when a reply is Arabic and when it is English.

The stored value is one of ``ARABIC_DIALECTS``; the empty string (or the key
being absent) means the merchant has not chosen one, and the platform then
behaves exactly as it did before the setting existed. It is stored in the
tenant settings' metadata (``REPLY_STYLE_KEY``), not among ``ai_settings``.

``ARABIC_DIALECT_MEANING`` and ``ARABIC_WITHOUT_DIALECT`` are the platform
meanings the commerce runtime hands the model as data in
``conversation_context`` (``services.commerce_runtime_pilot._reply_style_in``).
They are instructions to the model, never text sent to a customer. The legacy
path does not read them: it keeps ``tenant_overlay.LANGUAGE_MAP`` as it is.

Dependency-free on purpose, so the settings API, the runtime and the tests all
read one definition.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# The values the settings API accepts, in the order the dashboard offers them.
ARABIC_DIALECTS: Tuple[str, ...] = ("saudi", "iraqi", "egyptian", "levantine", "fusha")

# What each chosen dialect asks of an Arabic reply.
ARABIC_DIALECT_MEANING: Dict[str, str] = {
    "saudi": (
        "عند الرد بالعربية اكتب باللهجة السعودية العامية الطبيعية التي يفهمها "
        "أي عميل سعودي، دون مفردات من لهجات أخرى."
    ),
    "iraqi": (
        "عند الرد بالعربية اكتب باللهجة العراقية العامية الطبيعية، "
        "دون مفردات من لهجات أخرى."
    ),
    "egyptian": (
        "عند الرد بالعربية اكتب باللهجة المصرية العامية الطبيعية، "
        "دون مفردات من لهجات أخرى."
    ),
    "levantine": (
        "عند الرد بالعربية اكتب باللهجة الشامية العامية الطبيعية، "
        "دون مفردات من لهجات أخرى."
    ),
    "fusha": "عند الرد بالعربية اكتب بالعربية الفصحى الواضحة، دون عامية.",
}

# The "arabic" language choice without the dialect its platform meaning
# (``tenant_overlay.LANGUAGE_MAP["arabic"]``) names. Used only when the merchant
# chose a dialect, so the two settings never contradict each other.
ARABIC_WITHOUT_DIALECT = (
    "تحدث بالعربية دائماً. إذا بدأ العميل بالإنجليزية أو طلب التحدث بالإنجليزية، "
    "انتقل للإنجليزية."
)


def chosen_arabic_dialect(value: Any) -> Optional[str]:
    """The saved dialect when it is exactly one the platform defines, else ``None``.

    Exact, as the settings API accepts it: a value the API would refuse is not
    one the runtime quietly honours. ``None`` covers "not chosen" (missing,
    ``None``, blank) and any value the platform gives no meaning to; the
    caller decides whether the latter is worth reporting.
    """
    return value if isinstance(value, str) and value in ARABIC_DIALECTS else None


# Where the choice is stored: the tenant settings' own metadata, under its own
# namespace — not among ``ai_settings``, whose keys the legacy path hands its
# model wholesale. The legacy path does not apply a dialect, so it must not be
# shown one either.
REPLY_STYLE_KEY = "reply_style"
DIALECT_KEY = "arabic_dialect"


def stored_arabic_dialect(extra_metadata: Any) -> str:
    """The saved choice as the settings page shows it: a defined dialect, or ""."""
    block = extra_metadata.get(REPLY_STYLE_KEY) if isinstance(extra_metadata, dict) else None
    value = block.get(DIALECT_KEY) if isinstance(block, dict) else None
    return chosen_arabic_dialect(value) or ""


def with_arabic_dialect(extra_metadata: Any, value: str) -> Dict[str, Any]:
    """``extra_metadata`` with the choice set to ``value`` ("" clears it)."""
    meta = dict(extra_metadata) if isinstance(extra_metadata, dict) else {}
    block = dict(meta.get(REPLY_STYLE_KEY) or {}) if isinstance(meta.get(REPLY_STYLE_KEY), dict) else {}
    if value:
        block[DIALECT_KEY] = value
    else:
        block.pop(DIALECT_KEY, None)
    meta[REPLY_STYLE_KEY] = block
    return meta


# Which of this tenant's conversations the setting reaches. Only the commerce
# runtime reads it; the legacy path keeps its own Saudi baseline.
REACH_ALL = "all_conversations"
REACH_SOME = "runtime_conversations"
REACH_NONE = "none"


def arabic_dialect_reach(tenant_id: Any) -> str:
    """Whether the commerce runtime answers this tenant's conversations at all,
    and for all of them or some, by the checks its admission
    (``pilot_guard.evaluate_pilot_route``) applies to a tenant: enabled, a
    model configured, the tenant admitted by the mode, and who decides the
    recipient. In ``store_gated`` and ``global`` the store's own AI setting
    decides, so every conversation the store's AI answers is the runtime's; in
    ``pilot`` only the operator's recipients are, and none without any."""
    from core.commerce_runtime import pilot_guard as pg  # noqa: PLC0415

    try:
        tenant = int(tenant_id)
    except (TypeError, ValueError):
        return REACH_NONE
    if not pg.pilot_enabled() or not pg.pilot_model():
        return REACH_NONE
    if pg.runtime_mode() == pg.MODE_GLOBAL:
        return REACH_NONE if tenant in pg.global_tenant_denylist() else REACH_ALL
    if tenant not in pg.tenant_allowlist():
        return REACH_NONE
    if pg.store_gate_decides_recipient():
        return REACH_ALL
    return REACH_SOME if pg.recipient_allowlist() else REACH_NONE


__all__ = [
    "ARABIC_DIALECTS",
    "ARABIC_DIALECT_MEANING",
    "ARABIC_WITHOUT_DIALECT",
    "DIALECT_KEY",
    "REACH_ALL",
    "REACH_NONE",
    "REACH_SOME",
    "REPLY_STYLE_KEY",
    "arabic_dialect_reach",
    "chosen_arabic_dialect",
    "stored_arabic_dialect",
    "with_arabic_dialect",
]
