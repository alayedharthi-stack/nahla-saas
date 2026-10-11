"""
Strict parsing and topic classification of signed compliance bodies.

The HMAC covers the raw body only; the topic header, the route and every
other header are unsigned. So the action is derived from the **signed body's
own shape** and the claimed topic (route and header) must merely agree with
it. Each topic has distinguishing required and forbidden top-level fields:

  customers/data_request  requires shop_id, shop_domain, customer{…},
                          orders_requested[…] (may be empty) and
                          data_request{id}; forbids orders_to_redact
  customers/redact        requires shop_id, shop_domain, customer{…} and
                          orders_to_redact[…] (may be empty); forbids
                          data_request and orders_requested
  shop/redact             requires shop_id, shop_domain; forbids customer,
                          orders_requested, orders_to_redact, data_request

A body carrying markers of two topics, or an incomplete set, classifies as
nothing and is refused: a retained signed data request can never become a
redact or a shop redaction by changing the unsigned path or header, and no
signed body is actionable under a topic it was not built for.

A ``customer`` object may identify the customer by id, email or phone; the
id is optional (a record may carry only an email). Nothing returned here
contains the email, phone, customer id or order ids — only the strongest
identifier *kind* and the number of order ids.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.shopify_connection.shop_domain import canonical_shop_domain
from services.shopify_privacy.config import MAX_EMAIL_CHARS, MAX_ORDER_IDS, MAX_PHONE_CHARS
from services.shopify_privacy.models import (
    REF_EMAIL,
    REF_ID,
    REF_NONE,
    REF_PHONE,
    TOPIC_CUSTOMERS_DATA_REQUEST,
    TOPIC_CUSTOMERS_REDACT,
    TOPIC_SHOP_REDACT,
    TOPICS,
)

_ID_LIMIT = 10 ** 20
# A persisted id must fit PostgreSQL's signed BIGINT; a larger one is a
# deterministic refusal, never a storage retry.
_BIGINT_MAX = 2 ** 63 - 1

# Fixed refusal codes (the only text that leaves this module).
C_PAYLOAD_INVALID = "payload_invalid"
C_TOPIC_BODY_MISMATCH = "topic_body_mismatch"
C_SHOP_IDENTITY_INVALID = "shop_identity_invalid"


class PayloadRejected(Exception):
    """A refusal with a fixed code. ``str()`` is the code only — never input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PrivacyPayload:
    """What is kept of one signed compliance body: no raw customer identifier
    or payload. The shop identity and the Shopify request id remain linkable
    metadata (covered by the pending retention policy)."""

    topic: str
    shop_id: int
    shop_domain: str
    customer_ref_kind: Optional[str] = None
    orders_count: Optional[int] = None
    data_request_id: Optional[int] = None

    @property
    def shop_gid(self) -> str:
        return f"gid://shopify/Shop/{self.shop_id}"


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PayloadRejected(C_PAYLOAD_INVALID)
        out[key] = value
    return out


def _no_constants(_name: str) -> Any:
    raise PayloadRejected(C_PAYLOAD_INVALID)  # NaN / Infinity are not JSON


def parse_json_object(raw: bytes) -> Dict[str, Any]:
    """A strict UTF-8 JSON object: duplicate keys, NaN/Infinity and any other
    top-level value are refused."""
    try:
        text = bytes(raw).decode("utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except PayloadRejected:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PayloadRejected(C_PAYLOAD_INVALID) from None
    if not isinstance(value, dict):
        raise PayloadRejected(C_PAYLOAD_INVALID)
    return value


def classify(payload: Dict[str, Any]) -> Optional[str]:
    """The topic the signed body's own top-level shape belongs to, or None
    when it carries conflicting or incomplete markers."""
    keys = set(payload)
    has_data_request = "data_request" in keys
    has_requested = "orders_requested" in keys
    has_to_redact = "orders_to_redact" in keys
    has_customer = "customer" in keys
    if has_data_request or has_requested:
        if has_to_redact or not (has_data_request and has_requested and has_customer):
            return None
        return TOPIC_CUSTOMERS_DATA_REQUEST
    if has_to_redact:
        return TOPIC_CUSTOMERS_REDACT if has_customer else None
    if has_customer:
        return None  # a customer without its topic's distinguishing field
    return TOPIC_SHOP_REDACT


def _positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < _ID_LIMIT


def _shop_identity(payload: Dict[str, Any]) -> Tuple[int, str]:
    shop_id = payload.get("shop_id")
    if not _positive_id(shop_id):
        raise PayloadRejected(C_SHOP_IDENTITY_INVALID)
    raw_domain = payload.get("shop_domain")
    domain = canonical_shop_domain(raw_domain)
    # Exactly the canonical form: no case folding of a signed value.
    if domain is None or domain != raw_domain:
        raise PayloadRejected(C_SHOP_IDENTITY_INVALID)
    return shop_id, domain


def _order_ids(value: Any) -> int:
    """A bounded array of positive integer order ids (empty allowed); its length."""
    if not isinstance(value, list) or len(value) > MAX_ORDER_IDS:
        raise PayloadRejected(C_PAYLOAD_INVALID)
    if not all(_positive_id(v) for v in value):
        raise PayloadRejected(C_PAYLOAD_INVALID)
    return len(value)


def _optional_text(value: Any, limit: int) -> bool:
    """True when a non-empty string is present; None/absent is False; any
    other type or an oversized value is refused."""
    if value is None:
        return False
    if not isinstance(value, str) or len(value) > limit:
        raise PayloadRejected(C_PAYLOAD_INVALID)
    return bool(value.strip())


def _customer_ref_kind(value: Any) -> str:
    if not isinstance(value, dict):
        raise PayloadRejected(C_PAYLOAD_INVALID)
    customer_id = value.get("id")
    if customer_id is not None and not _positive_id(customer_id):
        raise PayloadRejected(C_PAYLOAD_INVALID)
    has_email = _optional_text(value.get("email"), MAX_EMAIL_CHARS)
    has_phone = _optional_text(value.get("phone"), MAX_PHONE_CHARS)
    if customer_id is not None:
        return REF_ID
    if has_email:
        return REF_EMAIL
    if has_phone:
        return REF_PHONE
    return REF_NONE


def parse_privacy_body(raw: bytes, claimed_topic: str) -> PrivacyPayload:
    """Validate a signed body against the topic claimed by the (unsigned)
    route and header. The body's own classification must equal it."""
    if claimed_topic not in TOPICS:
        raise PayloadRejected(C_TOPIC_BODY_MISMATCH)
    payload = parse_json_object(raw)
    if classify(payload) != claimed_topic:
        raise PayloadRejected(C_TOPIC_BODY_MISMATCH)
    shop_id, domain = _shop_identity(payload)
    if claimed_topic == TOPIC_SHOP_REDACT:
        return PrivacyPayload(topic=claimed_topic, shop_id=shop_id, shop_domain=domain)
    kind = _customer_ref_kind(payload["customer"])
    if claimed_topic == TOPIC_CUSTOMERS_REDACT:
        return PrivacyPayload(topic=claimed_topic, shop_id=shop_id, shop_domain=domain, customer_ref_kind=kind,
                              orders_count=_order_ids(payload["orders_to_redact"]))
    request = payload["data_request"]
    if not isinstance(request, dict) or not _positive_id(request.get("id")) or request["id"] > _BIGINT_MAX:
        raise PayloadRejected(C_PAYLOAD_INVALID)
    return PrivacyPayload(topic=claimed_topic, shop_id=shop_id, shop_domain=domain, customer_ref_kind=kind,
                          orders_count=_order_ids(payload["orders_requested"]),
                          data_request_id=int(request["id"]))
