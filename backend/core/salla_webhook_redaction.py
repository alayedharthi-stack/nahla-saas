"""
core/salla_webhook_redaction.py
───────────────────────────────
Decide what of a Salla webhook body may be persisted — fail closed.

Why
===
Salla delivers App Settings events (``app.settings.updated`` and the legacy
``app.settings``) with every configured field value in clear text, including
Password-type fields such as ``handoffSecret`` (the App Function → backend
HMAC key). Password masking in the Salla UI does not apply to the webhook
body. ``routers/webhooks.py`` used to store that body verbatim in
``webhook_events.raw_body`` / ``parsed_payload`` on the accepted,
rejected-signature, malformed and retry paths, and logged a 200-byte raw
preview of unparseable bodies.

Contract
========
* The caller verifies the signature, checks replay and builds the event
  identity from the ORIGINAL bytes and parse. This module never sees the
  signature; it only decides what may be stored.
* ``app.settings*`` events are projected: envelope/identity metadata and the
  Quick-Setup fields Nahla applies keep their values; every other value — a
  known secret or a field we do not know — becomes ``REDACTED``. Key names
  stay so operators can see which fields were present; ``None`` / ``""``
  stay because they hold nothing and show that a field is unset.
* In every other event, any ``settings`` object and any secret-named key
  (``*secret*``, ``*password*`` …) are redacted wherever they appear. Bodies
  with neither are persisted byte-identical, so order, cart, product and
  communication-observation processing is unchanged. OAuth ``access_token``
  / ``refresh_token`` are deliberately not matched: the dispatcher applies
  them from ``parsed_payload`` for ``app.store.authorize``.
* Whenever anything is redacted, the event is an ``app.settings*`` event, or
  the body repeats a JSON key (raw bytes and parse could disagree), the
  persisted ``raw_body`` is the re-serialised sanitised JSON — never the
  original bytes. No prefix, length, digest or fingerprint of the original
  body is produced here: given the rest of the body, a digest would be an
  offline-guessing oracle for a weak secret.
* A body that is not a JSON object, or cannot be sanitised, is never
  persisted, previewed or quoted. Error codes carry only the exception class
  or JSON type name.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union

# Same fixed marker as ``core.log_redaction.REDACTED``.
REDACTED = "REDACTED"

POLICY = "salla-redaction-v1"
# Authoritative marker written into ``webhook_events.raw_headers`` so an
# operator knows whether ``raw_body`` holds the received bytes. Any
# client-supplied header of the same name is dropped.
BODY_POLICY_HEADER = "x-nahla-body-policy"
BODY_ORIGINAL = "original"
BODY_SANITIZED = "sanitized"
BODY_WITHHELD = "withheld"

# Quick-Setup fields Nahla applies from App Settings
# (``routers.salla_app_settings.SallaQuickSetupIn``). A test pins this set
# to the model, so a newly processed field cannot be redacted silently.
PROCESSED_SETTINGS_KEYS = frozenset({
    "nahla_enabled",
    "whatsapp_number",
    "reply_tone",
    "abandoned_cart_enabled",
    "discount_percentage",
    "autopilot_enabled",
})

# Scalar metadata kept on ``app.settings*`` events for routing/diagnosis.
_SETTINGS_EVENT_ENVELOPE_KEYS = frozenset({
    "event", "merchant", "store_id", "created_at",
    "event_id", "webhook_event_id", "webhook_id",
})
_SETTINGS_EVENT_DATA_KEYS = frozenset({
    "id", "app_id", "app_name", "app_type", "merchant", "store_id",
    "created_at", "updated_at", "status",
})

# Matched against the key lower-cased with every non-alphanumeric removed,
# so ``handoffSecret``, ``handoff_secret`` and ``HANDOFF-SECRET`` all match.
_SECRET_KEY_FRAGMENTS = (
    "secret", "password", "passwd", "passphrase", "privatekey",
    "signingkey", "apikey", "hmackey", "encryptionkey",
)
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def is_secret_key(key: Any) -> bool:
    normalized = _NON_ALNUM.sub("", str(key).lower())
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def _is_settings_key(key: Any) -> bool:
    return str(key).strip().lower() == "settings"


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


@dataclass(frozen=True)
class SallaPersistableBody:
    """What a Salla receiver may store for one request body."""

    body_policy: str
    # Safe to persist: the original bytes (``original``), sanitised JSON
    # (``sanitized``) or ``None`` (``withheld``). Bodies stay out of repr:
    # an ``original`` body may still hold e.g. OAuth tokens it must carry.
    raw_body: Union[bytes, str, None] = field(repr=False)
    parsed_payload: Optional[Dict[str, Any]] = field(repr=False)
    # Safe diagnostic code; never contains body content.
    error: Optional[str] = None
    # The unsanitised parse, for in-memory routing and event identity only.
    # Never persist or log it; excluded from repr so it cannot leak that way.
    original_payload: Optional[Dict[str, Any]] = field(
        default=None, repr=False, compare=False,
    )

    @property
    def policy_marker(self) -> str:
        return f"{POLICY}:{self.body_policy}"


class _Redactor:
    def __init__(self) -> None:
        self.redactions = 0

    def mask(self, value: Any) -> Any:
        if value is None or value == "" or value == {} or value == []:
            return value
        self.redactions += 1
        return REDACTED

    def processed_value(self, value: Any) -> Any:
        if _is_scalar(value):
            return value
        # A processed field with a non-scalar value is invalid input. Keep it
        # invalid (the dispatcher's model still rejects it) without keeping
        # its content, and never as a plain marker string a str field would
        # accept as data.
        self.redactions += 1
        return [REDACTED]

    def settings(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return self.mask(value)
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key in PROCESSED_SETTINGS_KEYS and not is_secret_key(key):
                out[key] = self.processed_value(item)
            else:
                out[key] = self.mask(item)
        return out

    def tree(self, value: Any) -> Any:
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for key, item in value.items():
                if is_secret_key(key):
                    out[key] = self.mask(item)
                elif _is_settings_key(key):
                    out[key] = self.settings(item)
                else:
                    out[key] = self.tree(item)
            return out
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        return value

    def settings_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, item in payload.items():
            if key == "data":
                out[key] = self._settings_event_data(item)
            elif (
                key in _SETTINGS_EVENT_ENVELOPE_KEYS
                and not is_secret_key(key)
                and _is_scalar(item)
            ):
                out[key] = item
            else:
                out[key] = self.mask(item)
        return out

    def _settings_event_data(self, data: Any) -> Any:
        if not isinstance(data, dict):
            return self.mask(data)
        out: Dict[str, Any] = {}
        for key, item in data.items():
            if _is_settings_key(key):
                out[key] = self.settings(item)
            elif is_secret_key(key):
                out[key] = self.mask(item)
            elif key in _SETTINGS_EVENT_DATA_KEYS and _is_scalar(item):
                out[key] = item
            elif key in PROCESSED_SETTINGS_KEYS:
                # Legacy flat shape: ``data`` itself is the settings object
                # (the dispatcher reads ``data.get("settings") or data``).
                out[key] = self.processed_value(item)
            else:
                out[key] = self.mask(item)
        return out


def _withheld(error: str) -> SallaPersistableBody:
    return SallaPersistableBody(
        body_policy=BODY_WITHHELD,
        raw_body=None,
        parsed_payload=None,
        error=error,
    )


def sanitize_salla_webhook_body(raw_body: Union[bytes, str, None]) -> SallaPersistableBody:
    """Return what may be persisted for one Salla webhook request body.

    Parses exactly as ``Request.json()`` does (``json.loads`` on the received
    bytes), additionally noting repeated keys.
    """
    repeated_keys = False

    def _pairs(pairs):
        nonlocal repeated_keys
        obj: Dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                repeated_keys = True
            obj[key] = value
        return obj

    try:
        original = json.loads(raw_body or b"", object_pairs_hook=_pairs)
    except Exception as exc:  # noqa: BLE001 — body withheld; caller records the code
        return _withheld(f"invalid_json: {type(exc).__name__}")
    if not isinstance(original, dict):
        return _withheld(f"payload_not_object: {type(original).__name__}")

    try:
        redactor = _Redactor()
        event = str(original.get("event") or "").strip().lower()
        settings_event = event.startswith("app.settings")
        if settings_event:
            sanitized = redactor.settings_event(original)
        else:
            sanitized = redactor.tree(original)
        if not (settings_event or repeated_keys or redactor.redactions):
            return SallaPersistableBody(
                body_policy=BODY_ORIGINAL,
                raw_body=raw_body,
                parsed_payload=original,
                original_payload=original,
            )
        rewritten = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:  # noqa: BLE001 — e.g. RecursionError; body withheld
        return _withheld(f"payload_unsanitizable: {type(exc).__name__}")

    return SallaPersistableBody(
        body_policy=BODY_SANITIZED,
        raw_body=rewritten,
        parsed_payload=sanitized,
        original_payload=original,
    )


def headers_with_body_policy(headers: Any, body: SallaPersistableBody) -> Dict[str, Any]:
    """Request headers to persist, plus the authoritative body-policy marker."""
    out: Dict[str, Any] = {
        key: value
        for key, value in dict(headers or {}).items()
        if str(key).lower() != BODY_POLICY_HEADER
    }
    out[BODY_POLICY_HEADER] = body.policy_marker
    return out


__all__ = [
    "BODY_ORIGINAL",
    "BODY_POLICY_HEADER",
    "BODY_SANITIZED",
    "BODY_WITHHELD",
    "POLICY",
    "PROCESSED_SETTINGS_KEYS",
    "REDACTED",
    "SallaPersistableBody",
    "headers_with_body_policy",
    "is_secret_key",
    "sanitize_salla_webhook_body",
]
