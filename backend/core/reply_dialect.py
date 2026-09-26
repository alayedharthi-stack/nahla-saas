"""The merchant's Arabic-dialect setting and what the platform means by each choice.

``ai_settings.arabic_dialect`` is independent of the reply language
(``ai_settings.default_language``): a merchant may pair any language choice with
any dialect. The dialect governs how an Arabic reply is written; the language
setting alone decides when a reply is Arabic and when it is English.

The stored value is one of ``ARABIC_DIALECTS``; the empty string (or the key
being absent) means the merchant has not chosen one, and the platform then
behaves exactly as it did before the setting existed.

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
    """The saved dialect when it is one the platform defines, else ``None``.

    ``None`` covers "not chosen" (missing, ``None``, blank) and any value the
    platform gives no meaning to; the caller decides whether the latter is
    worth reporting.
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in ARABIC_DIALECTS else None


__all__ = [
    "ARABIC_DIALECTS",
    "ARABIC_DIALECT_MEANING",
    "ARABIC_WITHOUT_DIALECT",
    "chosen_arabic_dialect",
]
