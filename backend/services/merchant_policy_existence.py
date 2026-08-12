"""
Pack A1 policy existence map — PRESENT / UNKNOWN only.

Without a Salla CMS completeness signal, KNOWN_ABSENT must not be inferred
from missing MerchantKnowledgeSection rows alone.

KNOWN_PRESENT requires at least one ACTIVE + AI-visible + customer-ready
authoritative section (shared readiness contract with retrieval).
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
      KNOWN_PRESENT — active AI-visible customer-ready MKS row of that kind exists
      UNKNOWN — no authoritative complete section established

    KNOWN_ABSENT is never emitted in profile-only A1.
    Incomplete authoring/template sections do not establish PRESENT.
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
        from services.merchant_knowledge_customer_readiness import (  # noqa: PLC0415
            mks_section_customer_ready,
        )
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
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
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
        if kind in present:
            continue
        try:
            verdict = mks_section_customer_ready(row)
        except Exception:  # noqa: BLE001 — fail closed: do not treat as PRESENT
            continue
        if not verdict.is_ready:
            try:
                logger.info(
                    "merchant_knowledge_incomplete_skipped "
                    "surface=policy_existence knowledge_kind=%s source=%s "
                    "doc_ref=mks:%s reason_code=%s",
                    kind,
                    str(getattr(row, "source", "") or "unknown"),
                    getattr(row, "id", None),
                    verdict.reason_code or "incomplete",
                )
            except Exception:  # noqa: BLE001 — telemetry must not affect eligibility
                pass
            continue
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
