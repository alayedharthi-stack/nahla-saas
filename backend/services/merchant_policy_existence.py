"""
Pack A1 policy existence map — PRESENT / UNKNOWN only.

Without a Salla CMS completeness signal, KNOWN_ABSENT must not be inferred
from missing MerchantKnowledgeSection rows alone.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Policy kinds that may emit MERCHANT_POLICY existence facts.
POLICY_KIND_KEYS = (
    "return_policy",
    "refund_policy",
    "exchange_policy",
    "shipping_policy",
    "terms_policy",
    "privacy_policy",
    "warranty",
)


def build_policy_existence_map(
    db: Any,
    tenant_id: int,
    *,
    pages_sync_ok: Optional[bool] = None,  # ignored — no CMS completeness signal in A1
) -> Dict[str, Dict[str, Any]]:
    """Build policy existence map for Pack A1.

    Status values:
      KNOWN_PRESENT — active AI-visible MKS row of that kind exists
      UNKNOWN — no authoritative section established

    KNOWN_ABSENT is never emitted in profile-only A1.
    """
    del pages_sync_ok  # completeness reconcile deferred with CMS auto-import

    out: Dict[str, Dict[str, Any]] = {}
    for kind in POLICY_KIND_KEYS:
        out[kind] = {
            "status": "UNKNOWN",
            "doc_ref": None,
            "provenance": {
                "source": "merchant_knowledge",
                "tenant_id": int(tenant_id),
            },
        }

    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PackA1.policy_map] import failed: %s", exc)
        return out

    try:
        rows = (
            apply_ai_visible_kb_query_filters(
                db.query(MerchantKnowledgeSection)
            )
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(list(POLICY_KIND_KEYS)),
            )
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PackA1.policy_map] query failed tenant=%s: %s",
            tenant_id, exc,
        )
        return out

    present: Dict[str, Any] = {}
    for row in rows:
        kind = str(getattr(row, "kind", "") or "").strip().lower()
        if kind not in out:
            continue
        if kind not in present:
            present[kind] = row

    for kind, row in present.items():
        out[kind] = {
            "status": "KNOWN_PRESENT",
            "doc_ref": f"mks:{getattr(row, 'id', None)}",
            "provenance": {
                "source": str(getattr(row, "source", "") or "unknown"),
                "tenant_id": int(tenant_id),
            },
        }
    return out
