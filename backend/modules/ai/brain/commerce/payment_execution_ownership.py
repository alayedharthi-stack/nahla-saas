"""Payment execution ownership — Brain owns semantics; platform owns truth.

Weak lexical classifiers (``is_payment_query`` / bank-name substrings) may
rank merchant assets. They must not become customer semantic intent, outbound
consent, or customer-visible payment execution before Brain/LLM has owned
the unstructured turn.

Structured inbound (button / list / machine action IDs) may execute
deterministically because intent is already explicit in the payload.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

_STRUCTURED_INBOUND_TYPES = frozenset({"interactive", "button"})

# Machine action tokens only — never customer-language / phrase dictionaries.
_STRUCTURED_PAYMENT_ACTION_RE = re.compile(
    r"(?:^|[_\-:/])(?:payment|pay_now|iban|barcode|bank_transfer|"
    r"transfer_info|ask_payment|payment_asset)(?:$|[_\-:/])",
    re.IGNORECASE,
)

PAYMENT_BRAIN_TOPICS = frozenset({
    "payment_info",
    "payment_barcode_image",
    "merchant_payment_methods",
})

_ACTION_ID_KEYS = (
    "button_id",
    "wa_button_id",
    "button_provenance",
    "interactive_id",
    "list_reply_id",
    "nfm_reply_id",
)


def _meta(inbound_metadata: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    return inbound_metadata if isinstance(inbound_metadata, Mapping) else {}


def structured_action_ids(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
) -> tuple[str, ...]:
    meta = _meta(inbound_metadata)
    ids: list[str] = []
    for key in _ACTION_ID_KEYS:
        val = str(meta.get(key) or "").strip()
        if val:
            ids.append(val)
    for nested_key in ("button_reply", "list_reply", "nfm_reply"):
        nested = meta.get(nested_key)
        if isinstance(nested, Mapping):
            nid = str(nested.get("id") or "").strip()
            if nid:
                ids.append(nid)
    return tuple(ids)


def is_structurally_explicit_inbound(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
) -> bool:
    """True when the inbound is a machine payload, not free-text semantics."""
    ntype = str(
        normalized_type
        or _meta(inbound_metadata).get("normalized_type")
        or _meta(inbound_metadata).get("source_type")
        or ""
    ).strip().lower()
    if ntype in _STRUCTURED_INBOUND_TYPES:
        return True
    return bool(structured_action_ids(inbound_metadata))


def is_structurally_explicit_payment_action(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
) -> bool:
    """True when a structured payload already names a payment capability."""
    if not is_structurally_explicit_inbound(
        inbound_metadata, normalized_type=normalized_type,
    ):
        return False
    for token in structured_action_ids(inbound_metadata):
        if _STRUCTURED_PAYMENT_ACTION_RE.search(token):
            return True
    return False


def payment_early_bypass_allowed(
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    *,
    normalized_type: Optional[str] = None,
) -> bool:
    """Pre-Brain payment media/send is allowed only for structured payment actions.

    Unstructured natural language — including genuine payment asks — must reach
    Brain/LLM first. Asset existence is not authorization.
    """
    return is_structurally_explicit_payment_action(
        inbound_metadata, normalized_type=normalized_type,
    )


def brain_selected_payment_capability(
    brain_decision_args: Optional[Mapping[str, Any]] = None,
    *,
    brain_intent_name: str = "",
) -> bool:
    args = brain_decision_args if isinstance(brain_decision_args, Mapping) else {}
    topic = str(args.get("topic") or "").strip()
    if topic in PAYMENT_BRAIN_TOPICS:
        return True
    intent = str(brain_intent_name or args.get("intent") or "").strip().lower()
    return intent in {"ask_payment_info", "pay_now"}


def may_attach_payment_asset_after_brain(
    *,
    requestive_consent: bool,
    inbound_metadata: Optional[Mapping[str, Any]] = None,
    normalized_type: Optional[str] = None,
    brain_decision_args: Optional[Mapping[str, Any]] = None,
    brain_intent_name: str = "",
) -> bool:
    """Post-compose payment-asset attach.

    Brain/LLM already had the turn. Platform may execute the merchant asset
    only when:
    - the inbound is a structured payment action, or
    - requestive customer-origin consent is already true
      (not a weak ``is_payment_query`` bank-name collision).

    Asset existence is consulted only after this gate.
    """
    if is_structurally_explicit_payment_action(
        inbound_metadata, normalized_type=normalized_type,
    ):
        return True
    if not requestive_consent:
        return False
    return True


def asset_existence_creates_intent() -> bool:
    return False
