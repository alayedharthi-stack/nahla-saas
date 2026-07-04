"""Post-compose guard chain for FactBoundPersonaComposer."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from .facts_bundle import PersonaFactsBundle, PHASE2_SOCIAL_SURFACES
from .fallback_catalog import deterministic_fallback


@dataclass(frozen=True)
class PersonaGuardResult:
    text: str
    passed: bool
    failed_reason: str = ""
    repaired: bool = False


def _count_emojis(text: str) -> int:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    return sum(1 for ch in (text or "") if ch in PERSONA_ALLOWED_EMOJI)


def _strip_excess_emojis(text: str, *, max_emojis: int) -> tuple[str, bool]:
    from ..compose.persona_template_engine import PERSONA_ALLOWED_EMOJI  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    kept: list[str] = []
    emoji_seen = 0
    changed = False
    for ch in raw:
        if ch in PERSONA_ALLOWED_EMOJI:
            if emoji_seen < max_emojis:
                kept.append(ch)
                emoji_seen += 1
            else:
                changed = True
            continue
        kept.append(ch)
    return "".join(kept).strip(), changed


def _scrub_non_saudi_terms(text: str) -> tuple[str, bool]:
    from .policy_terms import NON_SAUDI_ARABIC_DIALECT_TERMS  # noqa: PLC0415

    raw = str(text or "")
    if not raw.strip():
        return raw, False
    changed = False
    cleaned = raw
    for term in NON_SAUDI_ARABIC_DIALECT_TERMS:
        pattern = re.compile(rf"(?<!\S){re.escape(term)}(?!\S)", re.UNICODE)
        if pattern.search(cleaned):
            cleaned = pattern.sub("", cleaned)
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _strip_known_customer_reasks(text: str, bundle: PersonaFactsBundle) -> tuple[str, bool]:
    from .policy_terms import (  # noqa: PLC0415
        KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES,
        KNOWN_CUSTOMER_NAME_REASK_PHRASES,
        KNOWN_CUSTOMER_PHONE_REASK_PHRASES,
    )

    ctx = bundle.customer_context or {}
    raw = str(text or "")
    if not raw.strip():
        return raw, False
    phrases: list[str] = []
    if ctx.get("has_verified_name"):
        phrases.extend(KNOWN_CUSTOMER_NAME_REASK_PHRASES)
    if ctx.get("has_whatsapp_phone"):
        phrases.extend(KNOWN_CUSTOMER_PHONE_REASK_PHRASES)
    if ctx.get("has_saved_address"):
        phrases.extend(KNOWN_CUSTOMER_BLUNT_ADDRESS_ASK_PHRASES)
    if not phrases:
        return raw, False
    changed = False
    cleaned = raw
    for phrase in phrases:
        if phrase in cleaned:
            cleaned = cleaned.replace(phrase, "").strip(" ،،.")
            changed = True
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, changed


def _truncate_safe(text: str, max_chars: int) -> str:
    raw = str(text or "").strip()
    if len(raw) <= max_chars:
        return raw
    cut = raw[:max_chars].rstrip()
    if cut and cut[-1] in "،.!?":
        return cut
    return cut.rstrip("،. ") + "…"


def apply_persona_compose_guards(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> PersonaGuardResult:
    """Run the fixed guard order from the rollout design doc."""
    working = str(text or "").strip()
    if not working:
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_compose")

    repaired = False
    lang = str(bundle.language or "ar").lower()

    # 1–2 Language / non-Saudi dialect + malformed كا suffix repair
    if lang.startswith("ar"):
        from .policy_terms import (  # noqa: PLC0415
            find_malformed_saudi_ka_suffix_tokens,
            find_non_saudi_arabic_terms,
            repair_malformed_saudi_ka_suffix,
        )

        repaired_ka, did_ka = repair_malformed_saudi_ka_suffix(working)
        if did_ka and repaired_ka.strip():
            working = repaired_ka
            repaired = True
        elif find_malformed_saudi_ka_suffix_tokens(working):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="malformed_saudi_ka_suffix",
            )

        if find_non_saudi_arabic_terms(working):
            scrubbed, did = _scrub_non_saudi_terms(working)
            if did and scrubbed.strip():
                working = scrubbed
                repaired = True
            elif find_non_saudi_arabic_terms(working):
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="non_saudi_dialect",
                )

    # 3 Credential / payment — immediate fallback, no repair
    from .policy_terms import looks_like_invented_payment_credential  # noqa: PLC0415

    if looks_like_invented_payment_credential(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="payment_credential",
        )
    try:
        from ..postprocess.payment_credential_guard import (  # noqa: PLC0415
            apply_payment_credential_guard,
        )

        pcg = apply_payment_credential_guard(
            working,
            db=db,
            tenant_id=tenant_id or bundle.tenant_id,
            inbound_text=bundle.inbound_text,
        )
        if pcg.replaced:
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="payment_credential_guard",
            )
        working = (pcg.reply or working).strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok — guard import must not break chain
        pass

    # 4 Fake operational claims on social surfaces
    if bundle.surface in PHASE2_SOCIAL_SURFACES:
        fake_markers = (
            "تم الشحن",
            "وصل الإيصال",
            "تم الدفع",
            "تم تأكيد الطلب",
        )
        if any(m in working for m in fake_markers):
            return PersonaGuardResult(
                text=working,
                passed=False,
                failed_reason="fake_operational_claim",
            )

    # 5 Checkout-pressure guard
    if bundle.surface in PHASE2_SOCIAL_SURFACES:
        try:
            from ..postprocess.social_checkout_pressure_guard import (  # noqa: PLC0415
                apply_social_checkout_pressure_guard,
            )

            scpg = apply_social_checkout_pressure_guard(
                working,
                inbound_text=bundle.inbound_text,
                tenant_id=tenant_id or bundle.tenant_id,
            )
            working = (scpg.reply or "").strip()
            if scpg.stripped and not working:
                return PersonaGuardResult(
                    text=working,
                    passed=False,
                    failed_reason="checkout_pressure_empty",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            pass

    # 6 Known customer re-ask
    working, did_reask = _strip_known_customer_reasks(working, bundle)
    if did_reask:
        repaired = True
    if not working.strip():
        return PersonaGuardResult(
            text="",
            passed=False,
            failed_reason="known_customer_reask_strip",
        )

    # 7 Emoji density
    max_emoji = int(bundle.constraints.max_emojis or 1)
    working, emoji_stripped = _strip_excess_emojis(working, max_emojis=max_emoji)
    if emoji_stripped:
        repaired = True
    from .policy_terms import rejects_fixed_emoji_template_opener  # noqa: PLC0415

    if rejects_fixed_emoji_template_opener(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="emoji_opener_spam",
        )

    # 8 Length
    if len(working) > bundle.constraints.max_chars:
        working = _truncate_safe(working, bundle.constraints.max_chars)
        repaired = True

    # 9 No silence
    if not working.strip():
        return PersonaGuardResult(text="", passed=False, failed_reason="empty_after_guards")

    from .policy_terms import rejects_social_support_bot_phrase  # noqa: PLC0415

    if bundle.surface in PHASE2_SOCIAL_SURFACES and rejects_social_support_bot_phrase(working):
        return PersonaGuardResult(
            text=working,
            passed=False,
            failed_reason="banned_support_bot_opener",
        )

    return PersonaGuardResult(
        text=working,
        passed=True,
        repaired=repaired,
    )


def apply_guards_or_fallback(
    text: str,
    bundle: PersonaFactsBundle,
    *,
    ctx: Any = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
) -> tuple[str, PersonaGuardResult]:
    """One repair attempt on dialect scrub failures, then deterministic fallback."""
    guard = apply_persona_compose_guards(
        text,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if guard.passed:
        return guard.text, guard

    if guard.failed_reason == "non_saudi_dialect":
        scrubbed, _ = _scrub_non_saudi_terms(text)
        if scrubbed.strip():
            retry = apply_persona_compose_guards(
                scrubbed,
                bundle,
                db=db,
                tenant_id=tenant_id,
            )
            if retry.passed:
                return retry.text, retry

    fb = deterministic_fallback(bundle, ctx=ctx, reason=guard.failed_reason)
    fb_guard = apply_persona_compose_guards(
        fb,
        bundle,
        db=db,
        tenant_id=tenant_id,
    )
    if fb_guard.passed and fb_guard.text.strip():
        return fb_guard.text, PersonaGuardResult(
            text=fb_guard.text,
            passed=False,
            failed_reason=guard.failed_reason,
        )
    emergency = unicodedata.normalize("NFKC", (fb or "حياك الله 😊").strip())
    return emergency, PersonaGuardResult(
        text=emergency,
        passed=False,
        failed_reason=guard.failed_reason or "fallback_failed",
    )
