"""Authority for delivery receipts from an exactly verified Meta body.

Legacy audit mode may process other webhook work without a valid signature.
Communication delivery state never inherits that audit-mode permission.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from core.webhook_security import verify_meta_signature

_MINT = object()


def _status_digest(status: dict[str, Any]) -> bytes:
    canonical = json.dumps(status, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


@dataclass(frozen=True, init=False)
class VerifiedMetaReceiptContext:
    _signed_statuses: frozenset[tuple[str, bytes]]

    def __init__(self, mint: object, signed_statuses: set[tuple[str, bytes]]):
        if mint is not _MINT:
            raise ValueError("verified_meta_receipt_context_required")
        object.__setattr__(self, "_signed_statuses", frozenset(signed_statuses))

    def permits(self, status: object, phone_number_id: str) -> bool:
        if not isinstance(status, dict) or not isinstance(phone_number_id, str) or not phone_number_id:
            return False
        try:
            return (phone_number_id, _status_digest(status)) in self._signed_statuses
        except (TypeError, ValueError):
            return False


def verify_receipt_context(
    raw_body: bytes, signature_header: str | None, *, secret: str | None,
) -> VerifiedMetaReceiptContext | None:
    verdict = verify_meta_signature(raw_body, signature_header, secret=secret)
    if not verdict.is_valid:
        return None
    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            return None
        signed_statuses: set[tuple[str, bytes]] = set()
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                phone_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
                if not phone_id:
                    continue
                for status in value.get("statuses", []):
                    if isinstance(status, dict):
                        signed_statuses.add((phone_id, _status_digest(status)))
    except (AttributeError, TypeError, ValueError):
        return None
    return VerifiedMetaReceiptContext(_MINT, signed_statuses)


def permits_receipt(
    context: object, status: object, phone_number_id: str,
) -> bool:
    return (
        type(context) is VerifiedMetaReceiptContext
        and context.permits(status, phone_number_id)
    )
