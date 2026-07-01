"""
payment_credential_guard.py
───────────────────────────
P0 — block invented IBAN / bank account / payment links in outbound replies.

Payment credentials may appear only when they match verified tenant settings
(``core.tenant_payment_accounts``). Placeholder or LLM-hallucinated values
are replaced with honest store-confirmation wording.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.postprocess.payment_credential_guard")

_PLACEHOLDER_IBAN = "SA1234567890123456789012"

_BANK_DETAILS_NOT_CONFIGURED_AR = (
    "بيانات التحويل البنكي غير مضبوطة في المتجر حالياً. "
    "تواصل مع المتجر لتأكيد بيانات التحويل، وبعد التحويل أرسل صورة الإيصال."
)

_INVENTED_CREDENTIAL_REPLY_AR = (
    "لا أقدر أرسل رقم حساب أو آيبان من هذه المحادثة إلا إذا كان مضبوطاً "
    "في إعدادات المتجر. تواصل مع المتجر لتأكيد بيانات التحويل."
)

_PAYMENT_LINK_RE = re.compile(
    r"https?://[^\s]+(?:pay|payment|checkout|moyasar|stripe|tap)[^\s]*",
    re.I,
)

_ACCOUNT_NUMBER_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"رقم\s*(?:ال)?(?:حساب|حساب(?:ك|نا)?)|"
    r"(?:ال)?(?:آيبان|ايبان|iban)"
    r")\s*[:\-–—]?\s*[^\n]+",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class PaymentCredentialGuardResult:
    reply: str
    replaced: bool = False
    reason: str = ""
    blocked_ibans: Tuple[str, ...] = ()


def _verified_ibans(db: Any, tenant_id: Optional[int]) -> Tuple[str, ...]:
    if db is None or not tenant_id:
        return ()
    try:
        from core.tenant_payment_accounts import load_tenant_payment_accounts  # noqa: PLC0415

        accounts = load_tenant_payment_accounts(db, tenant_id=int(tenant_id))
        return tuple(accounts.ibans or ())
    except Exception:  # noqa: BLE001
        return ()


def reply_contains_unverified_payment_credentials(
    reply: str,
    *,
    verified_ibans: Sequence[str],
) -> Tuple[bool, Tuple[str, ...]]:
    from core.tenant_payment_accounts import extract_ibans  # noqa: PLC0415

    raw = str(reply or "")
    if not raw.strip():
        return False, ()
    found = extract_ibans(raw)
    if not found and _PLACEHOLDER_IBAN.upper() in raw.upper().replace(" ", ""):
        found = [_PLACEHOLDER_IBAN]
    verified = {str(x).upper() for x in verified_ibans if x}
    blocked = tuple(ib for ib in found if ib.upper() not in verified)
    if blocked:
        return True, blocked
    if found and not verified:
        return True, tuple(found)
    return False, ()


def compose_verified_bank_transfer_block(
    db: Any,
    *,
    tenant_id: int,
) -> str:
    """Deterministic bank-transfer instructions from verified tenant settings only."""
    accounts = _verified_ibans(db, tenant_id)
    if not accounts:
        return _BANK_DETAILS_NOT_CONFIGURED_AR
    lines = ["تم اختيار التحويل البنكي."]
    if len(accounts) == 1:
        lines.append(f"الآيبان الخاص بالمتجر: {accounts[0]}")
    else:
        lines.append("حسابات التحويل المعتمدة للمتجر:")
        lines.extend(f"• {iban}" for iban in accounts)
    lines.append("إذا حولت، أرسل الإيصال هنا.")
    return "\n".join(lines)


def _strip_credential_lines(reply: str) -> str:
    text = str(reply or "")
    if not text.strip():
        return text
    cleaned = _ACCOUNT_NUMBER_LINE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def apply_payment_credential_guard(
    reply: str,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    inbound_text: str = "",
) -> PaymentCredentialGuardResult:
    original = str(reply or "")
    if not original.strip():
        return PaymentCredentialGuardResult(reply=original, replaced=False)

    verified = _verified_ibans(db, tenant_id)
    blocked, blocked_ibans = reply_contains_unverified_payment_credentials(
        original,
        verified_ibans=verified,
    )
    has_payment_link = bool(_PAYMENT_LINK_RE.search(original))

    if not blocked and not has_payment_link:
        return PaymentCredentialGuardResult(reply=original, replaced=False)

    replacement = _INVENTED_CREDENTIAL_REPLY_AR
    if blocked and not verified:
        replacement = _BANK_DETAILS_NOT_CONFIGURED_AR
    elif blocked:
        replacement = _INVENTED_CREDENTIAL_REPLY_AR

    cleaned = _strip_credential_lines(original)
    if cleaned and not reply_contains_unverified_payment_credentials(cleaned, verified_ibans=verified)[0]:
        if not _PAYMENT_LINK_RE.search(cleaned):
            logger.info(
                "[PAYMENT_CREDENTIAL_GUARD] stripped tenant=%s conversation=%s blocked=%s",
                tenant_id,
                conversation_id,
                list(blocked_ibans),
            )
            return PaymentCredentialGuardResult(
                reply=cleaned,
                replaced=True,
                reason="stripped_unverified_credentials",
                blocked_ibans=blocked_ibans,
            )

    logger.info(
        "[PAYMENT_CREDENTIAL_GUARD] replaced tenant=%s conversation=%s blocked=%s link=%s",
        tenant_id,
        conversation_id,
        list(blocked_ibans),
        has_payment_link,
    )
    return PaymentCredentialGuardResult(
        reply=replacement,
        replaced=True,
        reason="unverified_payment_credentials",
        blocked_ibans=blocked_ibans,
    )


__all__ = [
    "PaymentCredentialGuardResult",
    "apply_payment_credential_guard",
    "compose_verified_bank_transfer_block",
    "reply_contains_unverified_payment_credentials",
]
